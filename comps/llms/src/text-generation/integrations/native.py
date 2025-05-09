# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,  either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys

sys.path.append("/test/GenAIComps/")

import os
import threading
import time

import torch
from langchain_core.prompts import PromptTemplate

from comps import CustomLogger, GeneratedDoc, OpeaComponent, OpeaComponentRegistry, ServiceType
from comps.cores.proto.api_protocol import ChatCompletionRequest

from .template import ChatTemplate
from .utils import initialize_model

logger = CustomLogger("opea_textgen_native")
logflag = os.getenv("LOGFLAG", False)

MODEL_NAME = os.getenv("LLM_MODEL_ID", "Qwen/Qwen2-7B-Instruct")

input_sentences = [
    "DeepSpeed is a machine learning framework",
    "He is working on",
    "He has a",
    "He got all",
    "Everyone is happy and I can",
    "The new movie that got Oscar this year",
    "In the far far distance from our galaxy,",
    "Peace is the only way",
]

args_dict = {
    "device": "hpu",
    "model_name_or_path": MODEL_NAME,
    "bf16": True,
    "max_new_tokens": 32,
    "max_input_tokens": 128,
    "batch_size": 1,
    "warmup": 3,
    "n_iterations": 5,
    "local_rank": 0,
    "use_kv_cache": True,
    "use_hpu_graphs": True,
    "dataset_name": None,
    "column_name": None,
    "do_sample": False,
    "num_beams": 1,
    "trim_logits": False,
    "seed": 27,
    "profiling_warmup_steps": 0,
    "profiling_steps": 0,
    "profiling_record_shapes": False,
    "prompt": None,
    "bad_words": None,
    "force_words": None,
    "assistant_model": None,
    "peft_model": None,
    "num_return_sequences": 1,
    "token": None,
    "model_revision": "main",
    "attn_softmax_bf16": False,
    "output_dir": None,
    "bucket_size": -1,
    "bucket_internal": False,
    "dataset_max_samples": -1,
    "limit_hpu_graphs": False,
    "reuse_cache": False,
    "verbose_workers": False,
    "simulate_dyn_prompt": None,
    "reduce_recompile": False,
    "use_flash_attention": False,
    "flash_attention_recompute": False,
    "flash_attention_causal_mask": False,
    "flash_attention_fast_softmax": False,
    "book_source": False,
    "torch_compile": False,
    "ignore_eos": True,
    "temperature": 1.0,
    "top_p": 1.0,
    "top_k": None,
    "const_serialization_path": None,
    "disk_offload": False,
    "trust_remote_code": False,
    "quant_config": "",
    "world_size": 0,
    "show_graphs_count": False,
    "load_quantized_model_with_inc": False,
    "local_quantized_inc_model_path": None,
    "load_quantized_model_with_autogptq": False,
    "penalty_alpha": None,
}

if "Phi-4-mini-instruct" in MODEL_NAME:
    args_dict_phi4 = {
        "use_kv_cache": False,
        "attn_softmax_bf16": True,
        "limit_hpu_graphs": True,
        "use_flash_attention": True,
        "flash_attention_recompute": True,
        "flash_attention_causal_mask": True,
        "flash_attention_fast_softmax": True,
    }
    args_dict.update(args_dict_phi4)

if logflag:
    logger.info(args_dict)


class Args:
    def __init__(self, **entries):
        self.__dict__.update(entries)


model = None
assistant_model = None
tokenizer = None
generation_config = None
args = Args(**args_dict)
initialization_lock = threading.Lock()
initialized = False


def generate(
    input_query: list,
    device="hpu",
    max_new_tokens=32,
    use_lazy_mode=True,
    use_hpu_graphs=True,
    profiling_steps=0,
    profiling_warmup_steps=0,
    ignore_eos=True,
    profiling_record_shapes=False,
):
    """Generates sequences from the input sentences and returns them."""
    logger.info(f"[llm - generate] starting to inference with prompt {input_query}")
    encode_t0 = time.perf_counter()

    # Tokenization
    input_tokens = tokenizer.batch_encode_plus(
        input_query,
        return_tensors="pt",
        padding=True,
        return_token_type_ids=False,  # token_type_ids is not needed for falcon-three model
    )
    encode_duration = time.perf_counter() - encode_t0
    logger.info(f"[llm - generate] input tokenized: {input_tokens}")

    # Move inputs to target device(s)
    for t in input_tokens:
        logger.info(f"[llm - generate] t: {t}")
        if torch.is_tensor(input_tokens[t]):
            logger.info("[llm - generate] input[t] is tensor")
            logger.info(f"[llm - generate] device: {model.device}")
            input_tokens[t] = input_tokens[t].to(model.device)

    logger.info("[llm - generate] inputs transferred.")

    iteration_times = []
    outputs = model.generate(
        **input_tokens,
        generation_config=generation_config,
        assistant_model=assistant_model,
        max_new_tokens=max_new_tokens,
        lazy_mode=use_lazy_mode,
        hpu_graphs=use_hpu_graphs,
        profiling_steps=profiling_steps,
        profiling_warmup_steps=profiling_warmup_steps,
        ignore_eos=ignore_eos,
        iteration_times=iteration_times,
        profiling_record_shapes=profiling_record_shapes,
    ).cpu()
    logger.info("[llm - generate] result generated")
    first_token_time = iteration_times[0] + encode_duration
    result = tokenizer.batch_decode(outputs, skip_special_tokens=True)
    logger.info(f"[llm - generate] result: {result}")
    logger.info(f"[llm - generate] Time to first token = {first_token_time*1000}ms")
    return result


def initialize():
    global model, assistant_model, tokenizer, generation_config, initialized
    with initialization_lock:
        if not initialized:
            # initialize model and tokenizer
            import habana_frameworks.torch.hpu as torch_hpu
            from optimum.habana.utils import HabanaProfile

            model, assistant_model, tokenizer, generation_config = initialize_model(args, logger)
            logger.info("[llm] model and tokenizer initialized.")

            # compilation and model warmup
            HabanaProfile.disable()
            logger.info("[llm - native] Graph compilation...")
            for _ in range(args.warmup):
                generate(input_sentences)
            logger.info("[llm - native] model warm up finished.")
            torch_hpu.synchronize()
            HabanaProfile.enable()
            logger.info("[llm - native] Ready to inference")
            res = generate(["What is Deep Learning?"])
            logger.info(f"[llm - native] test result: {res}")
            initialized = True


@OpeaComponentRegistry.register("OpeaTextGenNative")
class OpeaTextGenNative(OpeaComponent):
    """A specialized OPEA TextGen component derived from OpeaComponent for interacting with LLM services based on native optimum habana."""

    def __init__(self, name: str, description: str, config: dict = None):
        super().__init__(name, ServiceType.LLM.name.lower(), description, config)
        initialize()
        health_status = self.check_health()
        if not health_status:
            logger.error("OpeaTextGenNative health check failed.")
        else:
            logger.info("OpeaTextGenNative health check success.")

    def check_health(self) -> bool:
        """Checks the health of the LLM service.

        Returns:
            bool: True if the service is reachable and healthy, False otherwise.
        """

        try:
            return initialized
        except Exception as e:
            logger.error(e)
            logger.error("Health check failed")
            return False

    async def invoke(self, input: ChatCompletionRequest):
        """Invokes the LLM service to generate output for the provided input.

        Args:
            input (ChatCompletionRequest): The input text(s).
        """

        message = None
        if isinstance(input.messages, str):
            message = input.messages
        else:  # List[Dict]
            for input_data in input.messages:
                if "role" in input_data and input_data["role"] == "user" and "content" in input_data:
                    message = input_data["content"]
                    if logflag:
                        logger.info(f"Get input text:\n {message}")
        if message is None:
            logger.error("Don't receive any input text, exit!")
            return GeneratedDoc(text=None, prompt=None)

        prompt = message
        prompt_template = None
        if input.chat_template:
            prompt_template = PromptTemplate.from_template(input.chat_template)
            input_variables = prompt_template.input_variables
        if prompt_template:
            if sorted(input_variables) == ["context", "question"]:
                prompt = prompt_template.format(question=message, context="\n".join(input.documents))
            elif input_variables == ["question"]:
                prompt = prompt_template.format(question=message)
            else:
                logger.info(f"{prompt_template} not used, we only support 2 input variables ['question', 'context']")
        else:
            if input.documents:
                prompt = ChatTemplate.generate_rag_prompt(message, input.documents)
        res = generate([prompt], max_new_tokens=input.max_tokens)

        if logflag:
            logger.info(f"[llm - native] inference result: {res}")
        return GeneratedDoc(text=res[0], prompt=message)
