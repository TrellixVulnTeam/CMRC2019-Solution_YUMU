#!/bin/bash
set -ex

python run_fixed_baseline.py \
	--cuda_devices 0,2,3 \
	--vocab_file ../bert_weights_chinese/vocab.txt \
	--bert_config_file ../bert_weights_chinese/bert_config.json \
	--init_checkpoint ../bert_weights_chinese/pytorch_model.bin \
	--do_train \
	--do_predict \
	--train_file ../data/cmrc2019_train.json \
	--predict_file ../data/cmrc2019_trial.json \
	--train_batch_size 24 \
	--learning_rate 2e-5 \
	--num_train_epochs 1.0 \
	--max_seq_length 512 \
	--gradient_accumulation_steps 2 \
	--output_dir ./fixed_baseline_output
