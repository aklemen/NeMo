# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import contextmanager
from typing import Any, Callable, Generator, List, Literal, Optional, Tuple, TypeVar, Union

import pytorch_lightning as pl
import torch
from lightning.pytorch.plugins.precision import MixedPrecision
from torch.nn import Module
from torch.optim import Optimizer

from nemo_ext.lightning._strategy_lib import GradScaler

AnyT = TypeVar("AnyT")


class MegatronMixedPrecision(MixedPrecision):
    def __init__(
        self,
        precision,
        amp_02: bool = True,
        device="cuda",
        scaler: Optional[Union[torch.cuda.amp.GradScaler, str]] = None,
    ) -> None:
        if precision == "bf16-mixed":
            scaler = None
        else:
            scaler = GradScaler(
                init_scale=2**32,
                growth_interval=1000,
                hysteresis=2,
            )

        super().__init__(precision, device, scaler)
        dtype = None
        # MixedPrecisionPlugin class in PTL >= 2.0 takes only "16-mixed" or "bf16-mixed" for precision arg
        if precision == "16-mixed":
            dtype = torch.float16

            def float16_convertor(val):
                return val.half()

        elif precision == "bf16-mixed":
            dtype = torch.bfloat16

            def float16_convertor(val):
                return val.bfloat16()

        self.dtype = dtype
        torch.set_autocast_gpu_dtype(dtype)
        self.float16_convertor = float16_convertor
        self.amp_02 = amp_02

    # def __init__(
    #     self,
    #     config: MegatronConfig,
    #     device="cuda",
    #     scaler: Optional[Union[torch.cuda.amp.GradScaler, str]] = None,
    # ) -> None:
    #     self.config = config
    #     if config.precision == "bf16-mixed":
    #         scaler = None
    #     else:
    #         scaler = GradScaler(
    #             init_scale=2**32,
    #             growth_interval=1000,
    #             hysteresis=2,
    #         )

    #     precision = cast(str, config.precision)
    #     super().__init__(precision, device, scaler)
    #     dtype = None
    #     # MixedPrecisionPlugin class in PTL >= 2.0 takes only "16-mixed" or "bf16-mixed" for precision arg
    #     if precision == "16-mixed":
    #         dtype = torch.float16

    #         def float16_convertor(val):
    #             return val.half()

    #     elif precision == "bf16-mixed":
    #         dtype = torch.bfloat16

    #         def float16_convertor(val):
    #             return val.bfloat16()

    #     torch.set_autocast_gpu_dtype(dtype)
    #     self.float16_convertor = float16_convertor

    def connect(
        self, model: Module, optimizers: List[Optimizer], lr_schedulers: List[Any]
    ) -> Tuple[Module, List[Optimizer], List[Any]]:
        """Connects this plugin to the accelerator and the training process."""
        from nemo.core.optim import MainParamsOptimizerWrapper
        
        if (
            not optimizers
            or not self.amp_02
            or isinstance(optimizers[0], MainParamsOptimizerWrapper)
        ):
            return model, optimizers, lr_schedulers

        _optimizers = [*optimizers]
        _optimizers[0] = self.convert_optimizer(_optimizers[0])

        return model, _optimizers, lr_schedulers
    
    # @override
    # def tensor_init_context(self) -> ContextManager:
    #     return _DtypeContextManager(self.dtype)

    # @override
    # def module_init_context(self) -> ContextManager:
    #     return _DtypeContextManager(self.dtype)

    def convert_module(self, module: Module) -> Module:
        """Convert the module parameters to the precision type this plugin handles.

        This is optional and depends on the precision limitations during optimization.

        """
        if self.precision == "bf16-mixed":
            return module.bfloat16()
        if self.precision == "16-mixed":
            return module.half()

        return module
    
    def convert_optimizer(self, optimizer: Optimizer) -> Optimizer:
        """Convert the optimizer parameters to the precision type this plugin handles.

        This is optional and depends on the precision limitations during optimization.

        """
        from nemo.core.optim import MainParamsOptimizerWrapper
        
        if isinstance(optimizer, MainParamsOptimizerWrapper) or not self.amp_02:
            return optimizer
        
        return MainParamsOptimizerWrapper(
            optimizer,
            # https://github.com/NVIDIA/NeMo/blob/main/nemo/collections/nlp/models/language_modeling/megatron_base_model.py#L496
            fp32_grad_accum=True,
            contiguous_grad_bucket=True,
        )

    def convert_input(self, data: AnyT) -> AnyT:
        """Convert model inputs (forward) to the floating point precision type of this plugin.

        Note: MegatronStrategy will take care of only doing this when:
            mpu.is_pipeline_first_stage()

        """
        from megatron.core.transformer.module import fp32_to_float16
        
        return fp32_to_float16(data, self.float16_convertor)

    def convert_output(self, data: AnyT) -> AnyT:
        """Convert outputs to the floating point precision type expected after model's forward.

        Note: MegatronStrategy will take care of only doing this when:
            mpu.is_pipeline_first_stage()

        """
        from megatron.core.transformer.module import float16_to_fp32
        
        return float16_to_fp32(data)

    def optimizer_step(
        self,
        optimizer: torch.optim.Optimizer,
        model: Union["pl.LightningModule", torch.nn.Module],
        closure: Callable[[], Any],
        **kwargs: Any,
    ) -> None:
        from nemo.core.optim import MainParamsOptimizerWrapper
        
        if not self.amp_02 and not isinstance(optimizer, MainParamsOptimizerWrapper):
            return super().optimizer_step(optimizer, model, closure, **kwargs)
        
        # assert isinstance(
        #     optimizer, MainParamsOptimizerWrapper
        # ), "MegatronHalfPrecisionPlugin supports only the optimizer with master parameters"

        if self.scaler is None:
            assert optimizer.fp32_grad_accumulation, "BF16 uses FP32 grad accumulation"
            _ = closure()
            self._after_closure(model, optimizer)
            return optimizer.step(**kwargs)

        assert not optimizer.fp32_grad_accumulation, "FP16 uses FP16 grad accumulation"
        closure_result = closure()

        # TODO: Add an option for merged all-reduce

        # cast fp16 grads to fp32 and copy to main grads, which are used for unscale and param update
        optimizer.copy_model_grads_to_main_grads()
        # `unscale` after the closure is executed but before the `on_before_optimizer_step` hook.
        # unscale main (fp32) gradients
        self.scaler.unscale_(optimizer)
        self._after_closure(model, optimizer)
        skipped_backward = closure_result is None
        # in manual optimization, the closure does not return a value
        if (
            not isinstance(model, pl.LightningModule)
            or not model.automatic_optimization
            or not skipped_backward
        ):
            # note: the scaler will skip the `optimizer.step` if nonfinite gradients are found
            self.scaler.step(optimizer, **kwargs)
            self.scaler.update()

    @contextmanager
    def forward_context(self) -> Generator[None, None, None]:
        """No explicit precision casting. Inputs are supposed to be manually casted."""
        try:
            yield
        finally:
            pass


# TODO: Remove this class
class PipelineMixedPrecision(MixedPrecision):
    """Overrides PTL autocasting to not wrap training/val/test_step.
    We do this because we have the megatron-core fwd/bwd functions in training_step.
    This means .backward is being called in training_step so we do not want the whole
    step wrapped in autocast.

    We instead wrap the fwd_output_and_loss_func that is passed to the megatron-core fwd/bwd functions.
    """

    def __init__(
        self,
        precision: Literal["16-mixed", "bf16-mixed"],
        device: str,
        scaler: Optional[torch.cuda.amp.GradScaler] = None,
    ) -> None:
        super().__init__(precision, device, scaler=scaler)
        dtype = None
        # MixedPrecisionPlugin class in PTL >= 2.0 takes only "16-mixed" or "bf16-mixed" for precision arg
        if precision == "16-mixed":
            dtype = torch.float16
        elif precision == "bf16-mixed":
            dtype = torch.bfloat16

        torch.set_autocast_gpu_dtype(dtype)

    @contextmanager
    def forward_context(self) -> Generator[None, None, None]:
        """Have the PTL context manager do nothing."""
        yield


# TODO: Remove this class
class MegatronHalfPrecision(MixedPrecision):
    """
    Plugin for Half (FP16 and BF16) precision training.
    This plugin assumes the use of the optimizer with master parameters (fp32).
    This plugin uses half-precision at all operators in the model so need of input precision
    at each layer operator.

    Args:
        precision: Whether to use ``torch.float16`` (``16``) or ``torch.bfloat16`` (``'bf16'``).
        device: The device for ``torch.autocast``.
        scaler: An optional :class:`torch.cuda.amp.GradScaler` to use.
    """

    def __init__(
        self,
        precision: Union[str, int],
        device: str,
        scaler: Optional[Union[torch.cuda.amp.GradScaler, str]] = None,
    ) -> None:
        if scaler == "auto":
            if precision == "bf16-mixed":
                scaler = None
            else:
                scaler = GradScaler(
                    init_scale=2**32,
                    growth_interval=1000,
                    hysteresis=2,
                )

        super().__init__(precision, device, scaler)
        dtype = None
        # MixedPrecisionPlugin class in PTL >= 2.0 takes only "16-mixed" or "bf16-mixed" for precision arg
        if precision == "16-mixed":
            dtype = torch.float16
        elif precision == "bf16-mixed":
            dtype = torch.bfloat16

        torch.set_autocast_gpu_dtype(dtype)

    def optimizer_step(
        self,
        optimizer: torch.optim.Optimizer,
        model: Union["pl.LightningModule", torch.nn.Module],
        closure: Callable[[], Any],
        **kwargs: Any,
    ) -> None:
        from nemo.core.optim import MainParamsOptimizerWrapper
        
        assert isinstance(
            optimizer, MainParamsOptimizerWrapper
        ), "MegatronHalfPrecisionPlugin supports only the optimizer with master parameters"

        if self.scaler is None:
            assert optimizer.fp32_grad_accumulation, "BF16 uses FP32 grad accumulation"
            _ = closure()
            self._after_closure(model, optimizer)
            return optimizer.step(**kwargs)

        assert not optimizer.fp32_grad_accumulation, "FP16 uses FP16 grad accumulation"
        closure_result = closure()

        # TODO: Add an option for merged all-reduce

        # cast fp16 grads to fp32 and copy to main grads, which are used for unscale and param update
        optimizer.copy_model_grads_to_main_grads()
        # `unscale` after the closure is executed but before the `on_before_optimizer_step` hook.
        # unscale main (fp32) gradients
        self.scaler.unscale_(optimizer)
        self._after_closure(model, optimizer)
        skipped_backward = closure_result is None
        # in manual optimization, the closure does not return a value
        if (
            not isinstance(model, pl.LightningModule)
            or not model.automatic_optimization
            or not skipped_backward
        ):
            # note: the scaler will skip the `optimizer.step` if nonfinite gradients are found
            self.scaler.step(optimizer, **kwargs)
            self.scaler.update()

    @contextmanager
    def forward_context(self) -> Generator[None, None, None]:
        """No explicit precision casting. Inputs are supposed to be manually casted."""
        try:
            yield
        finally:
            pass


__all__ = ["MegatronMixedPrecision", "MegatronHalfPrecision", "PipelineMixedPrecision"]
