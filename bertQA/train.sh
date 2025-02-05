#!/bin/bash
set -ex

python run_bertQA.py \
	--cuda_devices 1,2,3 \
	--vocab_file ../pretrained_weights/bert_weights_chinese/vocab.txt \
	--bert_config_file ../pretrained_weights/bert_weights_chinese/bert_config.json \
	--init_checkpoint ../pretrained_weights/bert_weights_chinese/pytorch_model.bin \
	--do_train \
	--do_evaluate \
	--train_file ../data/cmrc2019_train.json \
	--dev_file ../data/cmrc2019_dev.json \
	--train_batch_size 12 \
	--predict_batch_size 64 \
	--learning_rate 2e-5 \
	--num_train_epochs 10.0 \
	--max_seq_length 512 \
	--gradient_accumulation_steps 3 \
	--output_dir ./bertQA_output \
	--seed 777
