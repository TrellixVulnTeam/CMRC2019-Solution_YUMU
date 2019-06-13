#!/bin/bash
set -ex

python run_bertQA.py \
	--cuda_devices 1 \
	--vocab_file ../pretrained_weights/bert_weights_chinese/vocab.txt \
	--bert_config_file ../pretrained_weights/bert_weights_chinese/bert_config.json \
	--fine_tune_checkpoint ./bertQA_output/pytorch_model_best.bin \
	--do_predict \
	--predict_file ../data/cmrc2019_trial.json \
	--predict_batch_size 64 \
	--max_seq_length 512 \
	--output_dir ./bertQA_output
