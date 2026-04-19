# app/ad_facts.py
# SPDX-License-Identifier: Apache-2.0
"""Ad unit content: static Did-you-know facts and dynamic model recommendations."""
import random
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from compat_catalog import CompatCatalog
    from model_catalog import ModelCatalog

DID_YOU_KNOW: List[Dict[str, str]] = [
    {
        "headline": "Tensix: 108 independent compute cores per chip",
        "body": (
            "Each TT chip packs 108 Tensix cores in a 12×9 grid. "
            "Every core has 1.5 MB local SRAM and a matrix/vector unit that runs "
            "independently — no shared register file, no cache coherence overhead."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "100 Gb/s on-package Ethernet",
        "body": (
            "Chips connect via Ethernet fabric at 100 Gb/s. "
            "An allreduce across 4 chips takes ~1 µs — far less than one transformer "
            "layer's compute time. No proprietary interconnect license."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "TT Metal: zero Python overhead at inference",
        "body": (
            "TT Metal compiles ops into a static kernel-dispatch sequence. "
            "At inference time there is zero Python GIL overhead — the compiled "
            "trace replays RISC-V dispatches directly."
        ),
        "tag": "Software",
    },
    {
        "headline": "tt-forge: any PyTorch model on TT hardware",
        "body": (
            "tt-forge is a torch.compile backend. Any PyTorch model targets TT silicon "
            "without rewriting code — great for CNNs, vision transformers, and research "
            "models that don't need an OpenAI-compatible API server."
        ),
        "tag": "Ecosystem",
    },
    {
        "headline": "Paged KV attention keeps batches full",
        "body": (
            "KV cache is divided into 16-token blocks, eliminating fragmentation. "
            "New requests join mid-generation without stalling existing decodes — "
            "hardware utilization stays high."
        ),
        "tag": "Inference",
    },
    {
        "headline": "Trace capture = JIT compilation",
        "body": (
            "The trace_capture stage compiles separate graphs for 10 context lengths "
            "(128 → 65408 tokens). After capture, every inference replays a pre-built "
            "trace — no re-compilation per request."
        ),
        "tag": "Inference",
    },
    {
        "headline": "Wormhole vs Blackhole generations",
        "body": (
            "Wormhole (N150/N300/T3K) shipped 2022. "
            "Blackhole (P100/P150/P300) shipped 2024 with larger per-core SRAM, "
            "faster interconnect, and better int8 throughput."
        ),
        "tag": "Hardware",
    },
    {
        "headline": "Column-parallel weight sharding",
        "body": (
            "Attention matrices are sharded column-wise across chips: each holds "
            "Q_proj of shape [hidden × (hidden÷N)]. Activations flow over Ethernet "
            "fabric — not through host DRAM — reducing round-trip latency ~100×."
        ),
        "tag": "Architecture",
    },
    {
        "headline": "GQA reduces KV bandwidth 8×",
        "body": (
            "Grouped-Query Attention shares KV heads across query groups. "
            "Llama-3 uses 8 KV heads for 64 query heads — 8× smaller KV cache "
            "and matching reduction in decode memory bandwidth."
        ),
        "tag": "Architecture",
    },
    {
        "headline": "tt-smi: device management in one command",
        "body": (
            "'tt-smi -s' gives a JSON snapshot of temperature, utilization, and firmware. "
            "'tt-smi -r' soft-resets all TT devices — required when switching between "
            "model families that use incompatible memory layouts."
        ),
        "tag": "Workflow",
    },
    {
        "headline": "HF cache: no re-download after first pull",
        "body": (
            "Models cached in ~/.cache/huggingface are mounted read-only into the "
            "Docker container. Weights stream disk→chip at ~7 GB/s via PCIe — "
            "no internet required after the first download."
        ),
        "tag": "Workflow",
    },
    {
        "headline": "Continuous batching keeps utilization high",
        "body": (
            "vLLM fills finished decode slots immediately with new prefills, rather than "
            "waiting for the slowest request in the batch. Chip utilization stays high "
            "even with wildly uneven request lengths."
        ),
        "tag": "Inference",
    },

    # ── Model-specific cards ────────────────────────────────────────────────

    {
        "headline": "DeepSeek-R1 learned to reason without a teacher",
        "body": (
            "R1 was trained with reinforcement learning only — no supervised fine-tuning, "
            "no human-labeled reasoning chains. The 'aha moment' behavior (pausing, "
            "reconsidering) emerged spontaneously as the model maximized a math-correctness "
            "reward. AIME 2024 pass@1 improved from 15.6% → 71.0%."
        ),
        "tag": "Model",
    },
    {
        "headline": "DeepSeek-R1 uses GRPO — no critic model needed",
        "body": (
            "Standard RLHF trains a separate critic neural network. DeepSeek's GRPO (Group "
            "Relative Policy Optimization) skips it: correctness reward is computed from a "
            "group of sampled outputs. This halved GPU memory vs PPO and let DeepSeek train "
            "R1 at a fraction of the usual RL cost."
        ),
        "tag": "Model",
    },
    {
        "headline": "Llama 3 grew its vocabulary 50% over Llama 2",
        "body": (
            "Llama 3's tokenizer has 128 000 tokens — up from 32 000 in Llama 2. More tokens "
            "means fewer tokens per sentence, which means longer effective context for the same "
            "compute. Training used 15 trillion tokens, 7× the Llama 2 training corpus."
        ),
        "tag": "Model",
    },
    {
        "headline": "Llama 3's GQA cuts KV memory 8×",
        "body": (
            "All Llama 3 sizes use Grouped-Query Attention with 8 KV heads regardless of "
            "model scale. 64 query heads share 8 KV heads — 8× smaller KV cache than "
            "multi-head attention. Llama 2 70B had to add GQA retroactively; Llama 3 8B "
            "ships with it from day one."
        ),
        "tag": "Model",
    },
    {
        "headline": "FLUX.1 generates images from velocity, not noise",
        "body": (
            "Diffusion models traditionally predict the noise to subtract each step. "
            "FLUX.1's rectified flow instead predicts a velocity vector pointing straight "
            "from the noise distribution to the image distribution. The straighter path "
            "means fewer denoising steps — FLUX.1 [schnell] generates in 1–4 steps."
        ),
        "tag": "Model",
    },
    {
        "headline": "FLUX.1 [schnell]: 4 steps via adversarial distillation",
        "body": (
            "FLUX.1 [schnell] was distilled from the full [dev] model using latent adversarial "
            "diffusion distillation (LADD). A discriminator tells real from fake; the student "
            "model learns to fool it in 4 steps. Prompt-following accuracy: 94% vs 87% for "
            "DALL-E 3 on standard benchmarks."
        ),
        "tag": "Model",
    },
    {
        "headline": "Whisper handles 99 languages with one model",
        "body": (
            "Whisper was trained on 680 000 hours of weakly supervised audio — 50× more data "
            "than most ASR systems. It switches tasks via special tokens: <|transcribe|> for "
            "same-language text, <|translate|> for English output. Zero-shot, it matches or "
            "beats specialized models trained on individual languages."
        ),
        "tag": "Model",
    },
    {
        "headline": "Whisper's training data is 'weakly supervised'",
        "body": (
            "Unlike ASR datasets with carefully labeled transcriptions, Whisper trained on "
            "internet audio paired with whatever text happened to be alongside it — subtitles, "
            "captions, transcripts. This 'weak' labeling enabled 99-language coverage that "
            "would be impossible to curate manually."
        ),
        "tag": "Model",
    },
    {
        "headline": "Mixtral: 47B params, only 13B active per token",
        "body": (
            "Mixtral 8×7B has 8 expert feed-forward layers per transformer block; a router "
            "picks the top-2 for each token. Result: 47B total parameters but only 13B "
            "activate per forward pass — transformer-sized compute, 47B-parameter capacity. "
            "Caveat: all 47B must still fit in VRAM."
        ),
        "tag": "Model",
    },
    {
        "headline": "Mixtral experts specialize in syntax, not semantics",
        "body": (
            "Probing Mixtral's routing reveals experts cluster by token type — punctuation, "
            "Python keywords, French words — not by topic. The same expert processes a comma "
            "whether the sentence is about physics or recipes. Topics are handled by routing "
            "patterns across layers, not by single-expert specialization."
        ),
        "tag": "Model",
    },
    {
        "headline": "Mamba uses no attention — and scales to 1M context",
        "body": (
            "Mamba replaces self-attention with selective state spaces (S4). Instead of an "
            "N² attention matrix, it maintains a fixed-size hidden state updated per token. "
            "Inference memory is constant regardless of sequence length — a 1M-token context "
            "uses the same VRAM as a 100-token one. Throughput scales 5× faster than "
            "equivalent transformers."
        ),
        "tag": "Model",
    },
    {
        "headline": "Mamba's 'selective' state: the model decides what to forget",
        "body": (
            "Classic state-space models use fixed update matrices. Mamba makes B, C, and Δ "
            "functions of the current input token — the model dynamically decides how much "
            "of its state to carry forward vs reset. This selection mechanism lets Mamba "
            "ignore irrelevant context while retaining long-range dependencies."
        ),
        "tag": "Model",
    },
    {
        "headline": "Qwen3: switch reasoning mode mid-conversation",
        "body": (
            "Qwen3 models respond to /think to enter extended chain-of-thought mode and "
            "/no_think to switch back to fast direct responses — within a single session. "
            "You can budget compute per question rather than picking a model variant. "
            "Qwen3 was trained on 36 trillion tokens across 119 languages."
        ),
        "tag": "Model",
    },
    {
        "headline": "Qwen3-235B activates only 22B per token",
        "body": (
            "Qwen3-235B-A22B is a Mixture of Experts model: 235B total parameters, but only "
            "22B activate for each token. It benchmarks above GPT-4o and Claude Sonnet 3.7 "
            "on coding and math tasks. The 30B dense Qwen3 variant runs on a single A100 "
            "while matching GPT-4-class performance."
        ),
        "tag": "Model",
    },
    {
        "headline": "BGE unified three retrieval methods into one model",
        "body": (
            "BGE-M3 produces dense embeddings (cosine similarity), sparse BM25-style lexical "
            "scores, and multi-vector ColBERT representations — all from one forward pass. "
            "Each method excels in different regimes; hybrid ranking over all three beats any "
            "single method. BGE-M3 topped the MTEB retrieval leaderboard at release."
        ),
        "tag": "Model",
    },
    {
        "headline": "BGE handles 8192-token documents natively",
        "body": (
            "Most embedding models truncate at 512 tokens (one BERT page). BGE extends this "
            "to 8192 — enough for a long legal document or multi-turn conversation. The "
            "XLM-RoBERTa backbone supports 100+ languages, making BGE-M3 one of the few "
            "production retrievers that works multilingually at long context."
        ),
        "tag": "Model",
    },
    {
        "headline": "Stable Diffusion 3.5: text and image tokens attend to each other",
        "body": (
            "SD3.5's MMDiT (Multimodal Diffusion Transformer) processes text and noisy image "
            "tokens through the same attention layers. Image patches can directly attend to "
            "text tokens — and vice versa — giving the model precise control over how "
            "prompt words map to spatial regions."
        ),
        "tag": "Model",
    },
    {
        "headline": "Stable Diffusion 3.5 uses three text encoders",
        "body": (
            "SD3.5 conditions on T5-XXL (4.7B params), CLIP-L, and CLIP-G simultaneously. "
            "T5-XXL captures long-form semantic meaning; the CLIP encoders bring alignment "
            "with human image judgements. Dropping T5-XXL cuts quality for complex prompts "
            "significantly — text understanding is now compute-limited, not architecture-limited."
        ),
        "tag": "Model",
    },
    {
        "headline": "Falcon was trained on RefinedWeb — 5T deduplicated tokens",
        "body": (
            "TII built RefinedWeb by aggressively deduplicating CommonCrawl: URL filtering, "
            "fuzzy near-duplicate removal, and quality heuristics. The 5-trillion-token corpus "
            "was released publicly, allowing the community to reproduce Falcon's training data. "
            "Falcon 40B matched LLaMA 65B on most benchmarks using fewer training tokens."
        ),
        "tag": "Model",
    },
    {
        "headline": "Mistral uses Sliding Window Attention for long context",
        "body": (
            "Mistral 7B limits each token's attention window to 4096 neighbors — not the full "
            "sequence. Information propagates across layers: at layer 8, a token has effective "
            "receptive field of 4096 × 8 = 32K tokens. SWA delivers long-context behavior at "
            "standard-context compute cost."
        ),
        "tag": "Model",
    },
    {
        "headline": "ViT: images are sequences of patches, not pixels",
        "body": (
            "Vision Transformer splits an image into 16×16 pixel patches, flattens each into "
            "a vector, and feeds the sequence to a standard transformer. No convolutions — "
            "the attention mechanism learns spatial relationships from scratch. ViT-Large "
            "matches CNN accuracy with 4× fewer FLOPs at high resolution."
        ),
        "tag": "Model",
    },
    {
        "headline": "CLIP: 400M image-text pairs, zero supervised labels",
        "body": (
            "OpenAI's CLIP trained by predicting which image matches which caption — natural "
            "language as the label. No ImageNet categories. Zero-shot classification works "
            "by comparing 'a photo of a {class}' embeddings to image embeddings. CLIP "
            "representations transfer to 30+ downstream tasks without fine-tuning."
        ),
        "tag": "Model",
    },
    {
        "headline": "NBeats: time-series without any external features",
        "body": (
            "NBeats uses only the target time series as input — no date features, no "
            "seasonal dummies, no covariates. Stacked residual blocks learn trend and "
            "seasonality basis functions via backcast/forecast decomposition. It won the "
            "M4 competition and is one of the few models where interpretability and "
            "accuracy are both maximized."
        ),
        "tag": "Model",
    },
    {
        "headline": "SpeechT5: one model for speech input AND speech output",
        "body": (
            "SpeechT5 uses a unified encoder-decoder that handles ASR (speech→text), "
            "TTS (text→speech), voice conversion, and speech translation — all from the "
            "same pre-trained backbone. The trick: pre-net/post-net adapters convert "
            "between speech features and discrete tokens at the interface."
        ),
        "tag": "Model",
    },
]


def get_model_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return model-recommendation cards for the current device.

    Sources: existing ModelCatalog (tag='Model') + CompatCatalog for
    tt-forge/tt-metal extras that aren't in the server catalog (tag='Ecosystem').
    """
    cards: List[Dict[str, str]] = []

    if catalog and device_type:
        compatible = catalog.get_compatible([device_type]).all_entries()
        prod = [e for e in compatible if e.status == "PRODUCTION"]
        sample = random.sample(prod, min(4, len(prod))) if prod else compatible[:4]
        for e in sample:
            size_str = f"{e.param_count:.0f}B params  ·  " if e.param_count else ""
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{size_str}Engine: {e.inference_engine}  ·  "
                    f"Device: {e.device_type}  ·  Status: {e.status}"
                ),
                "tag": "Model",
            })

    if compat_catalog and device_type:
        server_names: set = set()
        if catalog:
            server_names = {e.display_name.lower() for e in catalog.all_entries()}
        extra = []
        for sw in ("tt-forge", "tt-metal"):
            for e in compat_catalog.get_for_hardware(device_type, software=sw):
                if e.display_name.lower() not in server_names and e.model_description:
                    extra.append((e, sw))
        random.shuffle(extra)
        for e, sw in extra[:3]:
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{e.model_description}\n"
                    f"Available via {sw} on {device_type} — run via Developer Image."
                ),
                "tag": "Ecosystem",
            })

    return cards


def get_all_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return a shuffled pool of static facts + model recommendations."""
    cards = list(DID_YOU_KNOW) + get_model_cards(catalog, device_type, compat_catalog)
    random.shuffle(cards)
    return cards
