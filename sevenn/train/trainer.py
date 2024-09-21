import os
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import torch
import torch.distributed as dist
import torch.nn
from torch.nn.parallel import DistributedDataParallel as DDP

import sevenn._keys as KEY
from sevenn.error_recorder import ErrorRecorder
from sevenn.train.loss import LossDefinition

from .loss import get_loss_functions_from_config
from .optim import optim_dict, scheduler_dict


class Trainer:
    """
    Training routine specialized for this package
    """

    def __init__(
        self,
        model: torch.nn.Module,
        loss_functions: List[Tuple[LossDefinition, float]],
        optimizer_cls,
        optimizer_args: Optional[dict] = None,
        scheduler_cls=None,
        scheduler_args: Optional[dict] = None,
        device: Union[torch.device, str] = 'auto',
        distributed: bool = False,
        distributed_backend: str = 'nccl',
    ):
        if device == 'auto':
            device = 'cuda' if torch.cuda.is_available() else 'cpu'

        if distributed:
            local_rank = int(os.environ['LOCAL_RANK'])
            self.rank = local_rank
            if distributed_backend == 'nccl':
                device = torch.device('cuda', local_rank)
                self.model = DDP(model.to(device), device_ids=[device])
            elif distributed_backend == 'mpi':
                self.model = DDP(model.to(device))
            else:
                raise ValueError(f'Unknown DDP backend: {distributed_backend}')
            dist.barrier()
            self.model.module.set_is_batch_data(True)
        else:
            self.model = model.to(device)
            self.model.set_is_batch_data(True)
            self.rank = 0

        self.device = device
        self.distributed = distributed

        param = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = optimizer_cls(param, **optimizer_args)
        if scheduler_cls is not None:
            self.scheduler = scheduler_cls(self.optimizer, **scheduler_args)
        else:
            self.scheduler = None
        self.loss_functions = loss_functions

    @staticmethod
    def from_config(model: torch.nn.Module, config: Dict[str, Any]) -> 'Trainer':
        trainer = Trainer(
            model,
            loss_functions=get_loss_functions_from_config(config),
            optimizer_cls=optim_dict[config[KEY.OPTIMIZER].lower()],
            optimizer_args=config[KEY.OPTIM_PARAM],
            scheduler_cls=scheduler_dict[config[KEY.SCHEDULER].lower()],
            scheduler_args=config[KEY.SCHEDULER_PARAM],
            device=config[KEY.DEVICE],
            distributed=config[KEY.IS_DDP],
            distributed_backend='nccl',
        )
        return trainer

    @staticmethod
    def args_from_checkpoint(checkpoint: str) -> Tuple[Dict, Dict, Dict]:
        """
        Usage:
            trainer_args, optim_stct, scheduler_stct = args_from_checkpoint('7net-0')
            # Do what you want to do here
            trainer = Trainer(**trainer_args)
            trainer.load_state_dict(
                optimizer_state_dict=optim_stct,
                scheduler_state_dict=scheduler_stct,
        """
        from sevenn.util import model_from_checkpoint, pretrained_name_to_path

        if os.path.isfile(checkpoint):
            checkpoint = checkpoint
        else:
            checkpoint = pretrained_name_to_path(checkpoint)

        cp = torch.load(checkpoint, weights_only=False)
        model, config = model_from_checkpoint(cp)
        optimizer_cls = optim_dict[config[KEY.OPTIMIZER].lower()]
        scheduler_cls = scheduler_dict[config[KEY.SCHEDULER].lower()]
        loss_functions = get_loss_functions_from_config(config)

        return (
            {
                'model': model,
                'loss_functions': loss_functions,
                'optimizer_cls': optimizer_cls,
                'optimizer_args': config[KEY.OPTIM_PARAM],
                'scheduler_cls': scheduler_cls,
                'scheduler_args': config[KEY.SCHEDULER_PARAM],
            },
            cp['optimizer_state_dict'],
            cp['scheduler_state_dict'],
        )

    def run_one_epoch(
        self,
        loader: Iterable,
        is_train: bool = False,
        error_recorder: Optional[ErrorRecorder] = None,
    ) -> None:
        if is_train:
            self.model.train()
        else:
            self.model.eval()

        for _, batch in enumerate(loader):
            if is_train:
                self.optimizer.zero_grad()
            batch = batch.to(self.device, non_blocking=True)
            output = self.model(batch)
            if error_recorder is not None:
                error_recorder.update(output)
            if is_train:
                total_loss = torch.tensor([0.0], device=self.device)
                for loss_def, w in self.loss_functions:
                    total_loss += loss_def.get_loss(output, self.model) * w
                total_loss.backward()
                self.optimizer.step()

        if self.distributed and error_recorder is not None:
            self.recorder_all_reduce(error_recorder)

    def scheduler_step(self, metric: Optional[float] = None) -> None:
        if self.scheduler is None:
            return
        if isinstance(self.scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
            assert isinstance(metric, float)
            self.scheduler.step(metric)
        else:
            self.scheduler.step()

    def get_lr(self) -> float:
        return float(self.optimizer.param_groups[0]['lr'])

    def recorder_all_reduce(self, recorder: ErrorRecorder) -> None:
        for metric in recorder.metrics:
            # metric.value._ddp_reduce(self.device)
            metric.ddp_reduce(self.device)

    def get_checkpoint_dict(self) -> dict:
        if self.distributed:
            model_state_dct = self.model.module.state_dict()
        else:
            model_state_dct = self.model.state_dict()
        return {
            'model_state_dict': model_state_dct,
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict()
            if self.scheduler is not None
            else None,
        }

    def write_checkpoint(self, path: str, **extra) -> None:
        if self.distributed and self.rank != 0:
            return
        cp = self.get_checkpoint_dict()
        cp.update(**extra)
        torch.save(cp, path)

    def load_state_dicts(
        self,
        model_state_dict: Optional[Dict] = None,
        optimizer_state_dict: Optional[Dict] = None,
        scheduler_state_dict: Optional[Dict] = None,
        strict: bool = True,
    ) -> None:
        if model_state_dict is not None:
            if self.distributed:
                self.model.module.load_state_dict(model_state_dict, strict=strict)
            else:
                self.model.load_state_dict(model_state_dict, strict=strict)

        if optimizer_state_dict is not None:
            self.optimizer.load_state_dict(optimizer_state_dict)
        if scheduler_state_dict is not None and self.scheduler is not None:
            self.scheduler.load_state_dict(scheduler_state_dict)
