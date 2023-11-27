import torch
from sfast.compilers.stable_diffusion_pipeline_compiler import CompilationConfig

from .module.stable_diffusion_pipeline_compiler import build_lazy_trace_module


def is_cuda_malloc_async():
    return "cudaMallocAsync" in torch.cuda.get_allocator_backend()


def gen_stable_fast_config():
    config = CompilationConfig.Default()
    # xformers and triton are suggested for achieving best performance.
    # It might be slow for triton to generate, compile and fine-tune kernels.
    try:
        import xformers

        config.enable_xformers = True
    except ImportError:
        print("xformers not installed, skip")
    try:
        import triton

        config.enable_triton = True
    except ImportError:
        print("triton not installed, skip")

    if config.enable_triton and is_cuda_malloc_async():
        print("disable stable fast triton because of cudaMallocAsync")
        config.enable_triton = False

    # CUDA Graph is suggested for small batch sizes.
    # After capturing, the model only accepts one fixed image size.
    # If you want the model to be dynamic, don't enable it.
    config.enable_cuda_graph = True
    # config.enable_jit_freeze = False
    return config


class StableFastPatch:
    def __init__(self, model, config):
        self.model = model
        self.config = config
        self.stable_fast_model = None

    def __call__(self, model_function, params):
        input_x = params.get("input")
        timestep_ = params.get("timestep")
        c = params.get("c")

        # disable with accelerate for now
        if hasattr(model_function.__self__, "hf_device_map"):
            return model_function(input_x, timestep_, **c)

        if self.stable_fast_model is None:
            self.stable_fast_model = build_lazy_trace_module(
                self.config,
                input_x.device,
                id(self),
            )

        return self.stable_fast_model.get_traced_module(
            model_function, input_x, timestep_, **c
        )(input_x, timestep_, **c)

    def to(self, device):
        if type(device) == torch.device:
            if self.config.enable_cuda_graph or self.config.enable_jit_freeze:
                if device.type == "cpu":
                    # comfyui tell we should move to cpu. but we cannt do it with cuda graph and freeze now.
                    del self.stable_fast_model
                    self.stable_fast_model = None
                    self.config.enable_cuda_graph = False
                    self.config.enable_jit_freeze = False
                    print(
                        "\33[93mWarning: Your graphics card doesn't have enough video memory to keep the model. Disable stable fast cuda graph, Flexibility will be improved but speed will be lost.\33[0m"
                    )
            else:
                if self.stable_fast_model != None and device.type == "cpu":
                    self.stable_fast_model.to_empty()
        return self


class ApplyStableFastUnet:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "enable_cuda_graph": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply_stable_fast"

    CATEGORY = "loaders"

    def apply_stable_fast(self, model, enable_cuda_graph):
        config = gen_stable_fast_config()

        if not enable_cuda_graph:
            config.enable_cuda_graph = False
            config.enable_jit_freeze = False

        if config.memory_format is not None:
            model.model.to(memory_format=config.memory_format)

        patch = StableFastPatch(model, config)
        model_stable_fast = model.clone()
        model_stable_fast.set_model_unet_function_wrapper(patch)
        return (model_stable_fast,)
