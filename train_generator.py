import json,os,time,argparse,warnings,time,yaml
from functools import partial
os.environ["TOKENIZERS_PARALLELISM"] = "false"
## torch
import torch
import torch.distributed as dist
## lightning
import pytorch_lightning as pl
from pytorch_lightning import LightningModule
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.callbacks import (
    ModelSummary,
    ModelCheckpoint,
    EarlyStopping,
)
from pytorch_lightning.utilities.warnings import PossibleUserWarning
warnings.filterwarnings("ignore", category=PossibleUserWarning)
## transformers
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    Adafactor,
)
## own
from utils.utils import (
    LabelSmoother,
    get_remain_time,
    get_gpu_usage,
)
from utils.metrics_utils import (
    get_rouge_score,
    get_bleu_score,
)
from utils.optim_utils import (
    get_inverse_sqrt_schedule_with_warmup
)
from summarization import (
    DualEncoderPegasusForConditionalGeneration,
    DualEncoderBartForConditionalGeneration,
)

class MemoryDataset(torch.utils.data.Dataset):

    def __init__(
        self,
        data,
        memory=None,
        ):
        super().__init__()
        self.data = data
        if memory is not None:
            assert len(data)==len(memory),(len(data),len(memory))
            for idx in range(len(data)):
                self.data[idx]['memory']=memory[idx]
    
    def __getitem__(self,index):
        return self.data[index]

    def __len__(self,):
        return len(self.data)

def collate_fct(samples,src_toker,trg_toker,max_src_len,max_trg_len,memory_encoding='concate',src='document',trg='summary'):
    
    src = [d[src] for d in samples]
    trg = [d[trg] for d in samples]

    tokenized_trg = trg_toker(trg,return_tensors='pt',padding=True,truncation=True,max_length=max_trg_len,return_attention_mask=False)
    tokenized_trg['input_ids'][tokenized_trg['input_ids']==trg_toker.pad_token_id]=-100
    
    has_memory = 'memory' in samples[0].keys()
    if not has_memory:

        tokenized_src = src_toker(src,return_tensors='pt',padding=True,truncation=True,max_length=max_src_len,return_attention_mask=True)
        return {
            "input_ids":tokenized_src['input_ids'],
            "attention_mask":tokenized_src['attention_mask'],
            'labels':tokenized_trg['input_ids'],
            "refs":trg,
            }

    else:
        memory = [d['memory'] for d in samples]
        if memory_encoding == 'concate':
            src = [[s,src_toker.eos_token + mem] for s,mem in zip(src,memory)]
            tokenized_src = src_toker(src,return_tensors='pt',padding=True,truncation=True,max_length=max_src_len,return_attention_mask=True)
            return {
                "input_ids":tokenized_src['input_ids'],
                "attention_mask":tokenized_src['attention_mask'],
                'labels':tokenized_trg['input_ids'],
                "refs":trg,
                }

        elif memory_encoding == 'separate':
            tokenized_memory = trg_toker(memory,return_tensors='pt',padding=True,truncation=True,max_length=max_trg_len)
            tokenized_src = trg_toker(src,return_tensors='pt',padding=True,truncation=True,max_length=max_src_len,return_attention_mask=True)
            return {
                "input_ids":tokenized_src['input_ids'],
                "attention_mask":tokenized_src['attention_mask'],
                'memory_input_ids':tokenized_memory['input_ids'],
                'memory_attention_mask':tokenized_memory['attention_mask'],
                'labels':tokenized_trg['input_ids'],
                "refs":trg,
            }

            
Metric2Fct = {
    "rouge":get_rouge_score,
    "bleu":get_bleu_score,
}

class ConditionalGenerator(LightningModule):
    @staticmethod
    def add_model_specific_args(parent_parser):
        
        parser = parent_parser.add_argument_group("model_args")
        ## data
        parser.add_argument('--data_dir')
        parser.add_argument('--config_path')
        parser.add_argument('--memory_dir')
        parser.add_argument('--memory_encoding')
        parser.add_argument('--src')
        parser.add_argument('--trg')
        parser.add_argument('--train_max_src_len',type=int)
        parser.add_argument('--train_max_trg_len',type=int)
        ## model
        parser.add_argument('--pretrained_model_path')
        ## generation
        parser.add_argument('--num_return_sequences',type=int)
        parser.add_argument('--num_beam_groups',type=int)
        parser.add_argument('--num_beams',type=int)
        parser.add_argument('--length_penalty',type=float)
        parser.add_argument('--diversity_penalty',type=float)
        parser.add_argument('--gen_max_len',type=int)
        parser.add_argument('--gen_min_len',type=int)
        parser.add_argument('--no_repeat_ngram_size',type=int)
        parser.add_argument('--early_stopping',type=bool)
        parser.add_argument('--top_p',type=float)
        parser.add_argument('--temperature',type=float)
        parser.add_argument('--do_sample',type=bool)
        ## training_parameters
        parser.add_argument('--lr',type=float)
        parser.add_argument('--warmup_steps',type=int)
        parser.add_argument('--weight_decay',type=float)
        parser.add_argument('--label_smoothing_factor',type=float)
        parser.add_argument('--per_device_train_batch_size',type=int)
        parser.add_argument('--per_device_eval_batch_size',type=int)
        parser.add_argument('--logging_steps',type=int)
        parser.add_argument('--eval_metrics')
        parser.add_argument('--seed',type=int)
        
        return parent_parser
    
    def __init__(self,*args,**kwargs):
        super().__init__()
        
        self.save_hyperparameters()
        self.configure_model()
        self.loss_fct = LabelSmoother()
        self.collate_fct = partial(collate_fct,
                                  src_toker=self.src_toker,trg_toker=self.trg_toker,
                                  max_src_len=self.hparams.train_max_src_len,
                                  max_trg_len=self.hparams.train_max_trg_len,
                                  src=self.hparams.src,trg=self.hparams.trg,
                                  memory_encoding=self.hparams.memory_encoding,
                                  )
        
        if self.hparams.eval_metrics == 'ppl':
            self.hparams.do_generation = False
        else:self.hparams.do_generation = True
        self.losses = []

    def configure_model(self):
        ## tokenizer
        self.src_toker = AutoTokenizer.from_pretrained(self.hparams.pretrained_model_path)
        self.trg_toker = self.src_toker ## to be compatible with NMT task
        self.vocab_size = self.trg_toker.vocab_size
        ## model
        if self.hparams.memory_dir is not None:
            ## retrieval-aug
            if self.hparams.memory_encoding == 'concate':
                self.model = AutoModelForSeq2SeqLM.from_pretrained(self.hparams.pretrained_model_path)
            elif self.hparams.memory_encoding == 'separate':
                if 'pegasus' in self.hparams.pretrained_model_path:
                    self.model = DualEncoderPegasusForConditionalGeneration.from_pretrained(self.hparams.pretrained_model_path)
                elif 'bart' in self.hparams.pretrained_model_path:
                    # config = BartConfig.from_pretrained(self.hparams.pretrained_model_path)
                    self.model = DualEncoderBartForConditionalGeneration.from_pretrained(self.hparams.pretrained_model_path)
        else:
            ## vanilla seq2seq
            self.model = AutoModelForSeq2SeqLM.from_pretrained(self.hparams.pretrained_model_path)

    def eval_generation(self,hyps,refs,stage='valid'):
        if stage == 'valid':
            cnt = self.valid_data_cnt
        elif stage == 'test':
            cnt = self.test_data_cnt
        hyps = hyps[:cnt]
        refs = refs[:cnt]
        r1,r2,rl = get_rouge_score(hyps,refs)
        bleu = get_bleu_score(hyps,refs)
        self.log(stage+"_rouge1",r1)
        self.log(stage+"_rouge2",r2)
        self.log(stage+"_rougeL",rl)
        self.log(stage+"_bleu",bleu)

    def get_mle_loss(self,batch,stage='fit'):

        epsilon = self.hparams.label_smoothing_factor if stage=='fit' else 0
        labels = batch.pop("labels")
        memory_kwargs = {}
        if 'memory_input_ids' in batch:
            memory_kwargs['memory_input_ids'] = batch['memory_input_ids']
            memory_kwargs['memory_attention_mask'] = batch['memory_attention_mask']
        output = self.model(
            input_ids=batch['input_ids'],
            attention_mask=batch['attention_mask'],
            decoder_input_ids=self.model.prepare_decoder_input_ids_from_labels(labels=labels),
            **memory_kwargs,
        )
        loss = torch.nn.functional.cross_entropy(output.logits.view(-1,self.vocab_size),labels.view(-1),label_smoothing=epsilon)
        return loss

    def training_step(self,batch,batch_idx):
        loss = self.get_mle_loss(batch,'fit')
        self.losses.append(loss.item())
        self.log("train_loss",loss,on_step=True,on_epoch=True,batch_size=batch['input_ids'].shape[0])
        return loss
    
    def test_step(self, batch, batch_idx):
        mle_loss = self.get_mle_loss(batch,'test').item()

        if self.hparams.do_generation:
            hyps = self.generate(batch)
            return hyps,batch['refs'],mle_loss
        else:
            return (mle_loss,)

    def validation_step(self,batch,batch_idx):
        mle_loss = self.get_mle_loss(batch,'valid').item()

        if self.hparams.do_generation:
            hyps = self.generate(batch)
            return hyps,batch['refs'],mle_loss
        else:
            return (mle_loss,)
    
    def merge(self,outputs):

        if dist.is_initialized():
            all_rank_outputs = [None for _ in range(dist.get_world_size())]    
            dist.all_gather_object(all_rank_outputs,outputs)
            outputs = [x for y in all_rank_outputs for x in y] ## all_rank_output[i]: i-th batch output
        single_batch_output_cnt = len(outputs[0])
        ret = [[] for _ in range(single_batch_output_cnt)]
        for idx in range(single_batch_output_cnt):
            for batch in outputs:
                ret[idx].append(batch[idx])
        return ret

    def test_epoch_end(self,outputs):
        if self.hparams.do_generation:
            hyps,refs,loss = self.merge(outputs)
            hyps = [x for y in hyps for x in y]
            refs = [x for y in refs for x in y]
            self.eval_generation(hyps,refs,'test')
        else:
            loss = self.merge(outputs)
        self.log("test_ppl",torch.mean(torch.exp(torch.tensor(loss))),sync_dist=False)
        self.log("test_loss",torch.mean(torch.tensor(loss)),sync_dist=False)

        if self.trainer.is_global_zero:
            if self.hparams.do_generation:
                with open(os.path.join(self.trainer.log_dir,'test_hyps.txt'),'w') as f:
                    for h in hyps[:self.test_data_cnt]:f.write(h.replace("\n"," ")+"\n")
                with open(os.path.join(self.trainer.log_dir,'test_refs.txt'),'w') as f:
                    for r in refs[:self.test_data_cnt]:f.write(r.replace("\n"," ")+"\n")
            model_type = os.path.basename(self.hparams.pretrained_model_path)
            self.model.save_pretrained(os.path.join(self.trainer.log_dir,model_type+'_best_ckpt'))
    
    def validation_epoch_end(self,outputs):
        if self.hparams.do_generation:
            hyps,refs,loss = self.merge(outputs)
            hyps = [x for y in hyps for x in y]
            refs = [x for y in refs for x in y]
            self.eval_generation(hyps,refs,'valid')
        else:
            loss = self.merge(outputs)
        self.log("valid_ppl",torch.mean(torch.exp(torch.tensor(loss))),sync_dist=False)
        self.log("valid_loss",torch.mean(torch.tensor(loss)),sync_dist=False)
        
    def on_train_start(self) -> None:
        self.train_start_time = time.time()
        self.print(self.hparams)
        if self.trainer.is_global_zero:
            model_type = os.path.basename(self.hparams.pretrained_model_path)
            self.src_toker.save_pretrained(os.path.join(self.trainer.log_dir,model_type+'_best_ckpt')) ## save here because weird problem on cluster

    def on_before_optimizer_step(self, optimizer, optimizer_idx: int) -> None:
        if self.global_step % self.hparams.logging_steps == 0 and self.global_step != 0 :
            msg  = f"{time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time()))} "
            msg += f"[{self.trainer.current_epoch}|{self.trainer.max_epochs}] "
            msg += f"[{self.global_step:6}|{self.trainer.estimated_stepping_batches}] "
            msg += f"Loss:{sum(self.losses)/len(self.losses):.4f} "
            msg += f"GPU Mem:{get_gpu_usage()} "
            self.losses = []
            msg += f"lr:{optimizer.param_groups[0]['lr']:e} "
            msg += f"remaining:{get_remain_time(self.train_start_time,self.trainer.estimated_stepping_batches,self.global_step)} "
            if 'valid_rouge1' in self.trainer.callback_metrics.keys():
                msg += f"valid_rouge1:{self.trainer.callback_metrics['valid_rouge1']:.4f} "
            if 'valid_ppl' in self.trainer.callback_metrics.keys():
                msg += f"valid_ppl:{self.trainer.callback_metrics['valid_ppl']:.4f} "
            if 'valid_bleu' in self.trainer.callback_metrics.keys():
                msg += f"valid_bleu:{self.trainer.callback_metrics['valid_bleu']:.4f} "
            self.print(msg)

    def configure_optimizers(self):
        optimizer = Adafactor(self.parameters(), scale_parameter=False, relative_step=False, warmup_init=False, lr=self.hparams.lr)
        lr_scheduler = get_inverse_sqrt_schedule_with_warmup(optimizer, self.hparams.warmup_steps)
        return {
                "optimizer": optimizer,
                "lr_scheduler": {
                    "scheduler": lr_scheduler,
                    "interval": "step",
                    },
                }
    
    def generate(self,batch):
        hyps = []
        with torch.no_grad():
            batch_size = batch['input_ids'].shape[0]
            additional_kwargs = {}
            if 'memory_input_ids' in batch.keys():
                additional_kwargs['memory_input_ids']=batch['memory_input_ids']
                additional_kwargs['memory_attention_mask']=batch['memory_attention_mask']
            if self.hparams.num_return_sequences is None:
                num_return_sequences = 1
            else:
                num_return_sequences=self.hparams.num_return_sequences * int(self.hparams.num_beams/self.hparams.num_beam_groups) if self.hparams.num_beam_groups is not None else self.hparams.num_return_sequences
            output = self.model.generate(
                input_ids=batch['input_ids'],
                attention_mask=batch['attention_mask'],
                max_length=self.hparams.gen_max_len+2,
                min_length=self.hparams.gen_min_len+1 if self.hparams.gen_min_len is not None else None,
                no_repeat_ngram_size=self.hparams.no_repeat_ngram_size,
                num_beams=self.hparams.num_beams,
                length_penalty=self.hparams.length_penalty,
                early_stopping=self.hparams.early_stopping,
                num_return_sequences=num_return_sequences,
                num_beam_groups=self.hparams.num_beam_groups, 
                diversity_penalty=self.hparams.diversity_penalty,
                top_p=self.hparams.top_p,
                temperature=self.hparams.temperature,
                do_sample=self.hparams.do_sample,
                **additional_kwargs
            )
            hyps = [self.trg_toker.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=False) for g in output]
            if self.hparams.num_beam_groups is not None and self.hparams.num_beam_groups > 1:
                num_return_candidates = int(self.hparams.num_return_sequences/self.hparams.num_beam_groups)
                hyps = [hyps[i] for i in range(len(hyps)) if i % num_return_candidates == 0]
        return hyps

    @staticmethod
    def reorder_ddp(all_rank_outputs):
        ## this function can only do with only 1 hyp
        rank_cnt = dist.get_world_size()
        num_data_per_rank = int(len(all_rank_outputs)/rank_cnt)
        output = []
        for idx in range(num_data_per_rank):
            output.extend([all_rank_outputs[i] for i in range(idx,len(all_rank_outputs),num_data_per_rank)])
        return output
            
    def load_data(self,_split):
        """
        This is for dataset construction
        Input: file_path(.jsonl)
        Output:
            -Dataset
            -number_of_data
            -reference(for valid/test)
        """
        data_path = os.path.join(self.hparams.data_dir,_split+".jsonl")
        data = [json.loads(x) for x in open(data_path).readlines()]
        data_cnt = len(data)
        memory = None
        if self.hparams.memory_dir is not None:
            mem_path = os.path.join(self.hparams.memory_dir,_split+".txt")
            memory = [x.strip() for x in open(mem_path).readlines()]
        dataset = MemoryDataset(
            data = data,
            memory = memory,
        )
        return data_cnt,dataset
    
    def setup(self,stage):
        if stage == 'fit':
            self.train_data_cnt,self.train_dataset=self.load_data('train')
            self.valid_data_cnt,self.valid_dataset=self.load_data('dev')
        # elif stage == 'valid':
        elif stage == 'test':
            self.test_data_cnt,self.test_dataset=self.load_data('test')
    
    def train_dataloader(self):
        return torch.utils.data.DataLoader(self.train_dataset, batch_size=self.hparams.per_device_train_batch_size,
                                           shuffle=True,collate_fn=self.collate_fct,
                                           num_workers=8, pin_memory=True)
    
    def val_dataloader(self):
        return torch.utils.data.DataLoader(self.valid_dataset, batch_size=self.hparams.per_device_eval_batch_size,
                                           shuffle=False,collate_fn=self.collate_fct,
                                           num_workers=8, pin_memory=True)
    
    def test_dataloader(self):
        return torch.utils.data.DataLoader(self.test_dataset, batch_size=self.hparams.per_device_eval_batch_size,
                                           shuffle=False,collate_fn=self.collate_fct,
                                           num_workers=8, pin_memory=True)




if __name__ == "__main__":
    
    ## args
    parser = argparse.ArgumentParser()
    parser.add_argument("--zero_shot",action='store_true')
    parser.add_argument("--do_not_train",action='store_true')
    parser.add_argument("--early_stop_patience",type=int,default=-1)
    
    parser = pl.Trainer.add_argparse_args(parser)
    parser = ConditionalGenerator.add_model_specific_args(parser)
    args = parser.parse_args()
    config = yaml.full_load(open(args.config_path))
    for k,v in config.items():
        if getattr(args,k) is None:
            setattr(args,k,v)
    ## seed
    pl.seed_everything(args.seed,workers=True)
    
    ## model
    model = ConditionalGenerator(**vars(args))
    
    ## strategy
    strategy = None
    if args.accelerator == 'gpu' and torch.cuda.device_count()>1:strategy = DDPStrategy(find_unused_parameters=False)

    ## callbacks
    monitor = "valid_"+args.eval_metrics
    mode = 'max' if args.eval_metrics != 'ppl' else 'min'
    callbacks = []
    callbacks.append(ModelCheckpoint(save_top_k=1, monitor=monitor,mode=mode))
    if args.early_stop_patience > -1:
        callbacks.append(EarlyStopping(monitor=monitor, mode=mode,patience=args.early_stop_patience))

    ## trainer
    trainer = pl.Trainer.from_argparse_args(
        args,
        callbacks= callbacks,
        strategy = strategy,
        val_check_interval=args.val_check_interval,
    )

    if args.zero_shot:
        trainer.test(model)
    
    if not args.do_not_train:
        trainer.fit(model)
        trainer.test()
    else:
        trainer.test(model)
    # trainer.test(model)