import os
# import math
import time
import logging
import argparse
import numpy as np

from chainer import cuda, global_config
import chainer.functions as F

from chainercv.utils import apply_to_iterator
from chainercv.utils import ProgressHook

from common.logger_utils import initialize_logging
from chainer_.top_k_accuracy import top_k_accuracy
from chainer_.utils import prepare_model

from chainer_.dataset_utils import get_dataset_metainfo
from chainer_.dataset_utils import get_val_data_source, get_test_data_source


def add_eval_parser_arguments(parser):
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="type of model to use. see model_provider for options")
    parser.add_argument(
        "--use-pretrained",
        action="store_true",
        help="enable using pretrained model from github repo")
    parser.add_argument(
        "--resume",
        type=str,
        default="",
        help="resume from previously saved parameters")
    parser.add_argument(
        "--data-subset",
        type=str,
        default="val",
        help="data subset. options are val and test")

    parser.add_argument(
        "--num-gpus",
        type=int,
        default=0,
        help="number of gpus to use")
    parser.add_argument(
        "-j",
        "--num-data-workers",
        dest="num_workers",
        default=4,
        type=int,
        help="number of preprocessing workers")

    parser.add_argument(
        "--batch-size",
        type=int,
        default=512,
        help="training batch size per device (CPU/GPU)")

    parser.add_argument(
        "--save-dir",
        type=str,
        default="",
        help="directory of saved models and log-files")
    parser.add_argument(
        "--logging-file-name",
        type=str,
        default="train.log",
        help="filename of training log")

    parser.add_argument(
        "--log-packages",
        type=str,
        default="chainer, chainercv",
        help="list of python packages for logging")
    parser.add_argument(
        "--log-pip-packages",
        type=str,
        default="cupy-cuda100, chainer, chainercv",
        help="list of pip packages for logging")

    parser.add_argument(
        "--disable-cudnn-autotune",
        action="store_true",
        help="disable cudnn autotune for segmentation models")
    parser.add_argument(
        "--show-progress",
        action="store_true",
        help="show progress bar")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Evaluate a model for image classification/segmentation (Chainer)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--dataset",
        type=str,
        default="ImageNet1K",
        help="dataset name. options are ImageNet1K, CUB200_2011, CIFAR10, CIFAR100, SVHN, VOC2012, ADE20K, Cityscapes, "
             "COCO")
    parser.add_argument(
        "--work-dir",
        type=str,
        default=os.path.join("..", "imgclsmob_data"),
        help="path to working directory only for dataset root path preset")

    args, _ = parser.parse_known_args()
    dataset_metainfo = get_dataset_metainfo(dataset_name=args.dataset)
    dataset_metainfo.add_dataset_parser_arguments(
        parser=parser,
        work_dir_path=args.work_dir)

    add_eval_parser_arguments(parser)

    args = parser.parse_args()
    return args


def prepare_ch_context(num_gpus):
    if num_gpus > 0:
        cuda.get_device(0).use()
    return num_gpus


def test(net,
         test_data,
         num_gpus,
         calc_weight_count=False,
         extended_log=False):
    tic = time.time()

    predictor = test_data["predictor_class"](base_model=net)
    if num_gpus > 0:
        predictor.to_gpu()

    if calc_weight_count:
        weight_count = net.count_params()
        logging.info("Model: {} trainable parameters".format(weight_count))

    in_values, out_values, rest_values = apply_to_iterator(
        predictor.predict,
        test_data["iterator"],
        hook=ProgressHook(test_data["ds_len"]))
    del in_values

    pred_probs, = out_values
    gt_labels, = rest_values

    y = np.array(list(pred_probs))
    t = np.array(list(gt_labels))

    top1_acc = F.accuracy(
        y=y,
        t=t).data
    top5_acc = top_k_accuracy(
        y=y,
        t=t,
        k=5).data
    err_top1_val = 1.0 - top1_acc
    err_top5_val = 1.0 - top5_acc

    if extended_log:
        logging.info("Test: err-top1={top1:.4f} ({top1})\terr-top5={top5:.4f} ({top5})".format(
            top1=err_top1_val, top5=err_top5_val))
    else:
        logging.info("Test: err-top1={top1:.4f}\terr-top5={top5:.4f}".format(
            top1=err_top1_val, top5=err_top5_val))
    logging.info("Time cost: {:.4f} sec".format(
        time.time() - tic))


def main():
    args = parse_args()

    if args.disable_cudnn_autotune:
        os.environ["MXNET_CUDNN_AUTOTUNE_DEFAULT"] = "0"

    _, log_file_exist = initialize_logging(
        logging_dir_path=args.save_dir,
        logging_file_name=args.logging_file_name,
        script_args=args,
        log_packages=args.log_packages,
        log_pip_packages=args.log_pip_packages)

    ds_metainfo = get_dataset_metainfo(dataset_name=args.dataset)
    ds_metainfo.update(args=args)
    assert (ds_metainfo.ml_type != "imgseg") or (args.batch_size == 1)
    assert (ds_metainfo.ml_type != "imgseg") or args.disable_cudnn_autotune

    global_config.train = False
    num_gpus = prepare_ch_context(args.num_gpus)

    net = prepare_model(
        model_name=args.model,
        use_pretrained=args.use_pretrained,
        pretrained_model_file_path=args.resume.strip(),
        net_extra_kwargs=ds_metainfo.net_extra_kwargs,
        num_gpus=num_gpus)
    assert (hasattr(net, "classes"))
    assert (hasattr(net, "in_size"))
    # num_classes = net.classes
    # input_image_size = net.in_size[0]

    if args.data_subset == "val":
        test_data = get_val_data_source(
            ds_metainfo=ds_metainfo,
            batch_size=args.batch_size)
        # test_metric = get_composite_metric(
        #     metric_names=ds_metainfo.val_metric_names,
        #     metric_extra_kwargs=ds_metainfo.val_metric_extra_kwargs)
    else:
        test_data = get_test_data_source(
            ds_metainfo=ds_metainfo,
            batch_size=args.batch_size)
        # test_metric = get_composite_metric(
        #     metric_names=ds_metainfo.test_metric_names,
        #     metric_extra_kwargs=ds_metainfo.test_metric_extra_kwargs)

    assert (args.use_pretrained or args.resume.strip())
    test(
        net=net,
        test_data=test_data,
        num_gpus=num_gpus,
        calc_weight_count=True,
        extended_log=True)


if __name__ == "__main__":
    main()
