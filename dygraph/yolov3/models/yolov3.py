#  Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

from __future__ import division
from __future__ import print_function

import paddle.fluid as fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.initializer import Constant
from paddle.fluid.initializer import Normal
from paddle.fluid.regularizer import L2Decay

from config import cfg

from paddle.fluid.dygraph.nn import Conv2D, BatchNorm
from .darknet import DarkNet53_conv_body
from .darknet import ConvBNLayer

from paddle.fluid.dygraph.base import to_variable

class YoloDetectionBlock(fluid.dygraph.Layer):
    def __init__(self,ch_in,channel,is_test=True):
        super(YoloDetectionBlock, self).__init__()

        assert channel % 2 == 0, \
            "channel {} cannot be divided by 2".format(channel)

        self.conv0 = ConvBNLayer(
            ch_in=ch_in,
            ch_out=channel,
            filter_size=1,
            stride=1,
            padding=0,
            is_test=is_test
            )
        self.conv1 = ConvBNLayer(
            ch_in=channel,
            ch_out=channel*2,
            filter_size=3,
            stride=1,
            padding=1,
            is_test=is_test
            )
        self.conv2 = ConvBNLayer(
            ch_in=channel*2,
            ch_out=channel,
            filter_size=1,
            stride=1,
            padding=0,
            is_test=is_test
            )
        self.conv3 = ConvBNLayer(
            ch_in=channel,
            ch_out=channel*2,
            filter_size=3,
            stride=1,
            padding=1,
            is_test=is_test
            )
        self.route = ConvBNLayer(
            ch_in=channel*2,
            ch_out=channel,
            filter_size=1,
            stride=1,
            padding=0,
            is_test=is_test
            )
        self.tip = ConvBNLayer(
            ch_in=channel,
            ch_out=channel*2,
            filter_size=3,
            stride=1,
            padding=1,
            is_test=is_test
            )
    def forward(self, inputs):
        out = self.conv0(inputs)
        out = self.conv1(out)
        out = self.conv2(out)
        out = self.conv3(out)
        route = self.route(out)
        tip = self.tip(route)
        return route, tip


class Upsample(fluid.dygraph.Layer):
    def __init__(self,scale=2):
        super(Upsample,self).__init__()
        self.scale = scale

    def forward(self, inputs):
        # get dynamic upsample output shape
        shape_nchw = fluid.layers.shape(inputs)
        shape_hw = fluid.layers.slice(shape_nchw, axes=[0], starts=[2], ends=[4])
        shape_hw.stop_gradient = True
        in_shape = fluid.layers.cast(shape_hw, dtype='int32')
        out_shape = in_shape * self.scale
        out_shape.stop_gradient = True

        # reisze by actual_shape
        out = fluid.layers.resize_nearest(
            input=inputs, scale=self.scale, actual_shape=out_shape)
        return out

class YOLOv3(fluid.dygraph.Layer):
    def __init__(self,ch_in,is_train=True, use_random=False):
        super(YOLOv3,self).__init__()

        self.is_train = is_train
        self.use_random = use_random

        self.block = DarkNet53_conv_body(ch_in=ch_in,
                                         is_test = not self.is_train)
        self.block_outputs = []
        self.yolo_blocks = []
        self.route_blocks_2 = []
        ch_in_list = [1024,768,384]
        for i in range(3):
            yolo_block = self.add_sublayer(
                "yolo_detecton_block_%d" % (i),
                YoloDetectionBlock(ch_in_list[i],
                                   channel = 512//(2**i),
                                   is_test = not self.is_train))
            self.yolo_blocks.append(yolo_block)

            num_filters = len(cfg.anchor_masks[i]) * (cfg.class_num + 5)

            block_out = self.add_sublayer(
                "block_out_%d" % (i),
                Conv2D(num_channels=1024//(2**i),
                       num_filters=num_filters,
                       filter_size=1,
                       stride=1,
                       padding=0,
                       act=None,
                       param_attr=ParamAttr(
                           initializer=fluid.initializer.Normal(0., 0.02)),
                       bias_attr=ParamAttr(
                           initializer=fluid.initializer.Constant(0.0),
                           regularizer=L2Decay(0.))))
            self.block_outputs.append(block_out)
            if i < 2:
                route = self.add_sublayer("route2_%d"%i,
                                          ConvBNLayer(ch_in=512//(2**i),
                                                      ch_out=256//(2**i),
                                                      filter_size=1,
                                                      stride=1,
                                                      padding=0,
                                                      is_test=(not self.is_train)))
                self.route_blocks_2.append(route)
            self.upsample = Upsample()

    def forward(self, inputs, gtbox=None, gtlabel=None, gtscore=None, im_id=None, im_shape=None ):
        self.outputs = []
        self.boxes = []
        self.scores = []
        self.losses = []
        self.downsample = 32
        blocks = self.block(inputs)
        for i, block in enumerate(blocks):
            if i > 0:
                block = fluid.layers.concat(input=[route, block], axis=1)
            route, tip = self.yolo_blocks[i](block)
            block_out = self.block_outputs[i](tip)
            self.outputs.append(block_out)

            if i < 2:
                route = self.route_blocks_2[i](route)
                route = self.upsample(route)
        self.gtbox = gtbox
        self.gtlabel = gtlabel
        self.gtscore = gtscore
        self.im_id = im_id
        self.im_shape = im_shape

        # cal loss
        for i,out in enumerate(self.outputs):
            anchor_mask = cfg.anchor_masks[i]
            if self.is_train:
                loss = fluid.layers.yolov3_loss(
                    x=out,
                    gt_box=self.gtbox,
                    gt_label=self.gtlabel,
                    gt_score=self.gtscore,
                    anchors=cfg.anchors,
                    anchor_mask=anchor_mask,
                    class_num=cfg.class_num,
                    ignore_thresh=cfg.ignore_thresh,
                    downsample_ratio=self.downsample,
                    use_label_smooth=cfg.label_smooth)
                self.losses.append(fluid.layers.reduce_mean(loss))

            else:
                mask_anchors = []
                for m in anchor_mask:
                    mask_anchors.append(cfg.anchors[2 * m])
                    mask_anchors.append(cfg.anchors[2 * m + 1])
                boxes, scores = fluid.layers.yolo_box(
                    x=out,
                    img_size=self.im_shape,
                    anchors=mask_anchors,
                    class_num=cfg.class_num,
                    conf_thresh=cfg.valid_thresh,
                    downsample_ratio=self.downsample,
                    name="yolo_box" + str(i))
                self.boxes.append(boxes)
                self.scores.append(
                    fluid.layers.transpose(
                        scores, perm=[0, 2, 1]))
            self.downsample //= 2



        if not self.is_train:
        # get pred
            yolo_boxes = fluid.layers.concat(self.boxes, axis=1)
            yolo_scores = fluid.layers.concat(self.scores, axis=2)

            pred = fluid.layers.multiclass_nms(
                bboxes=yolo_boxes,
                scores=yolo_scores,
                score_threshold=cfg.valid_thresh,
                nms_top_k=cfg.nms_topk,
                keep_top_k=cfg.nms_posk,
                nms_threshold=cfg.nms_thresh,
                background_label=-1)
            return pred
        else:
            return sum(self.losses)

