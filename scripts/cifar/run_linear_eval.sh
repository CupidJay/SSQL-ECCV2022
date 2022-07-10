python -m torch.distributed.launch --nproc_per_node=4 --use_env main.py \
    linear  --config ./configs/cifar/resnet18_linear_eval_cifar.yaml \
    --output /data/train_log_SSQL_release/cifar/resnet18_linear_eval_cifar10_3w3f -j 8