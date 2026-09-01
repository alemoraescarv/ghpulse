"""Deterministic, offline topical classifier for trending repos.

Pure Python, no network, no LLM, no randomness: the same repo dict always maps
to the same list of category ids. Used by :mod:`ghpulse.render` to tag both the
Repos cards and the News items so the client-side filter behaves identically in
both views.

The scoring model (per the council spec):

* +3 for every matching topic  (substring match for patterns registered as
  ``topics_sub``, exact match for ``topics``), capped at 6 per category.
* +2 for every description/name keyword hit (word-boundary regex for
  ``kw_wb`` terms, plain substring for ``kw_sub`` terms), capped at 4.
* +1 language hint, but ONLY when the category already has at least one topic
  or keyword hit — a language alone never tags a repo.

Categories scoring >= 3 survive; they are ordered by (-score, priority_index)
and the top 3 are kept. A small set of suppression rules then prune obviously
redundant tags. If nothing survives, the repo falls back to ``["general"]``.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Category tables
# ---------------------------------------------------------------------------
# Each category declares:
#   topics      exact topic matches            (+3, cap 6)
#   topics_sub  substring topic patterns       (+3, cap 6)  [spec's trailing *]
#   kw_wb       word-boundary keyword terms    (+2, cap 4)  [spec's \b terms]
#   kw_sub      substring keyword terms        (+2, cap 4)
#   langs       language hints                 (+1, gated on a topic/kw hit)

_CATEGORIES: list[dict[str, Any]] = [
    {
        "id": "ui-ux",
        "label": "UI/UX",
        "emoji": "🎨",
        "topics": [
            "ui", "ux", "design-system", "design-systems", "components",
            "component-library", "tailwind", "tailwindcss", "shadcn", "radix",
            "radix-ui", "css", "styling", "theme", "theming", "figma", "icons",
            "fonts", "typography", "animation", "animations", "motion",
            "storybook", "chakra", "material-ui", "mui", "ant-design", "daisyui",
            "styled-components", "design",
        ],
        "topics_sub": [],
        "kw_wb": [
            "ui", "ux", "css", "theme", "themes", "icon", "icons", "font",
            "fonts", "button", "buttons", "component", "components",
            "accessible", "accessibility",
        ],
        "kw_sub": [
            "design system", "component library", "tailwind", "shadcn", "radix",
            "styling", "storybook", "figma", "animation", "styled-components",
            "beautifully designed", "design language",
        ],
        "langs": ["css", "html"],
    },
    {
        "id": "ai-agents",
        "label": "AI Agents",
        "emoji": "🤖",
        "topics": [
            "agent", "agents", "ai-agents", "ai-agent", "autonomous-agents",
            "multi-agent", "agentic", "llm-agent", "agent-framework", "autogpt",
            "crewai", "react-agent", "tool-use", "function-calling", "mcp",
            "model-context-protocol",
        ],
        "topics_sub": ["agent"],
        "kw_wb": ["agent", "agents", "agentic", "autonomous", "multi-agent"],
        "kw_sub": [
            "ai agent", "agent framework", "autonomous agent", "function calling",
            "tool use", "tool-calling", "agent crew", "react agent",
            "orchestrate", "cooperative agent",
        ],
        "langs": ["python"],
    },
    {
        "id": "llm-infra",
        "label": "LLM Inference/Infra",
        "emoji": "⚡",
        "topics": [
            "llm", "llms", "inference", "cuda", "gpu", "quantization", "gguf",
            "vllm", "llama", "llama-cpp", "transformers", "tensorrt", "triton",
            "kv-cache", "serving", "model-serving", "ggml", "onnx",
            "tensor-parallel", "flash-attention", "mlx", "ollama",
        ],
        "topics_sub": [],
        "kw_wb": [
            "llm", "llms", "inference", "cuda", "gpu", "quantization",
            "quantized", "serving", "throughput", "vllm",
        ],
        "kw_sub": [
            "large language model", "language model", "llama.cpp",
            "flash attention", "kv cache", "model serving", "gpu inference",
            "on-device", "running large language models", "local llm",
        ],
        "langs": ["c++", "cuda", "python"],
    },
    {
        "id": "context-rag",
        "label": "Context/RAG/Memory",
        "emoji": "🧠",
        "topics": [
            "rag", "retrieval", "retrieval-augmented", "embeddings", "embedding",
            "vector-search", "vector-database", "semantic-search", "memory",
            "knowledge-base", "context", "long-context", "reranking", "chunking",
            "vectordb", "faiss", "langchain", "llamaindex",
        ],
        "topics_sub": [],
        "kw_wb": [
            "rag", "retrieval", "embeddings", "embedding", "memory", "reranking",
            "chunking",
        ],
        "kw_sub": [
            "retrieval-augmented", "vector search", "vector database",
            "semantic search", "knowledge base", "long context", "context-aware",
            "context window", "context aware",
        ],
        "langs": [],
    },
    {
        "id": "dev-tools",
        "label": "Dev Tools/CLI",
        "emoji": "🛠️",
        "topics": [
            "cli", "command-line", "developer-tools", "devtools", "terminal",
            "tooling", "productivity", "editor", "ide", "linter", "formatter",
            "build-tool", "package-manager", "dotfiles", "tui", "shell",
        ],
        "topics_sub": [],
        "kw_wb": ["cli", "terminal", "tui", "linter", "formatter", "editor", "repl"],
        "kw_sub": [
            "command-line", "command line", "developer tool", "dev tool",
            "package manager", "build tool", "productivity", "project manager",
        ],
        "langs": ["shell", "rust", "go"],
    },
    {
        "id": "web-frontend",
        "label": "Web/Frontend",
        "emoji": "🌐",
        "topics": [
            "web", "frontend", "react", "vue", "svelte", "angular", "nextjs",
            "next", "nuxt", "spa", "ssr", "browser", "dom", "jsx", "vite",
            "webpack", "htmx", "astro", "remix", "solidjs", "web-components",
        ],
        "topics_sub": [],
        "kw_wb": [
            "web", "frontend", "react", "vue", "svelte", "angular", "browser",
            "dom", "spa", "ssr",
        ],
        "kw_sub": [
            "front-end", "web framework", "single-page", "server-side rendering",
            "javascript framework", "web app", "web frontend",
        ],
        "langs": ["typescript", "javascript", "html"],
    },
    {
        "id": "backend-infra",
        "label": "Backend/Infra",
        "emoji": "🧱",
        "topics": [
            "backend", "server", "api", "rest", "grpc", "graphql",
            "microservices", "middleware", "http", "framework", "web-framework",
            "rpc", "message-queue", "event-driven", "distributed-systems",
            "load-balancer", "proxy", "gateway", "networking", "queue",
        ],
        "topics_sub": [],
        "kw_wb": [
            "backend", "server", "api", "rest", "grpc", "graphql",
            "microservices", "middleware", "rpc", "proxy", "gateway", "queue",
        ],
        "kw_sub": [
            "back-end", "web framework", "message queue", "event-driven",
            "distributed system", "load balancer", "http server", "reverse proxy",
            "job queue",
        ],
        "langs": ["go", "rust", "java"],
    },
    {
        "id": "data-ml",
        "label": "Data/ML",
        "emoji": "📊",
        "topics": [
            "machine-learning", "deep-learning", "ml", "data-science", "data",
            "dataset", "datasets", "neural-network", "neural-networks",
            "pytorch", "tensorflow", "pandas", "numpy", "analytics",
            "data-engineering", "etl", "dataframe", "notebook", "jupyter",
            "scikit-learn", "model-training", "training", "computer-vision", "nlp",
        ],
        "topics_sub": [],
        "kw_wb": [
            "ml", "dataset", "datasets", "analytics", "pandas", "numpy", "etl",
            "training", "classifier",
        ],
        "kw_sub": [
            "machine learning", "deep learning", "data science", "neural network",
            "data pipeline", "computer vision", "data engineering", "dataframe",
            "model training", "data pipelines",
        ],
        "langs": ["python", "r"],
    },
    {
        "id": "security",
        "label": "Security",
        "emoji": "🔒",
        "topics": [
            "security", "cybersecurity", "infosec", "pentesting", "pentest",
            "vulnerability", "exploit", "cryptography", "crypto", "encryption",
            "auth", "authentication", "authorization", "oauth", "jwt", "malware",
            "firewall", "scanner", "ctf", "appsec", "secrets", "tls", "ssl",
        ],
        "topics_sub": [],
        "kw_wb": [
            "security", "vulnerability", "exploit", "encryption", "cryptography",
            "malware", "pentest", "auth", "oauth", "jwt", "firewall", "scanner",
        ],
        "kw_sub": [
            "cyber security", "penetration testing", "authentication",
            "authorization", "secrets management", "supply chain",
        ],
        "langs": [],
    },
    {
        "id": "databases",
        "label": "Databases",
        "emoji": "🗄️",
        "topics": [
            "database", "databases", "sql", "nosql", "postgres", "postgresql",
            "mysql", "sqlite", "mongodb", "redis", "database-engine", "orm",
            "query-engine", "olap", "oltp", "columnar", "duckdb", "clickhouse",
            "key-value", "timeseries", "embedded-database",
        ],
        "topics_sub": [],
        "kw_wb": [
            "database", "databases", "sql", "nosql", "postgres", "mysql",
            "sqlite", "mongodb", "redis", "orm", "olap", "oltp", "duckdb",
        ],
        "kw_sub": [
            "query engine", "key-value store", "time series", "columnar store",
            "relational database", "database engine",
        ],
        "langs": [],
    },
    {
        "id": "devops-cloud",
        "label": "DevOps/Cloud",
        "emoji": "☁️",
        "topics": [
            "devops", "kubernetes", "k8s", "docker", "container", "containers",
            "terraform", "ansible", "ci-cd", "cicd", "cloud", "aws", "gcp",
            "azure", "helm", "gitops", "observability", "monitoring",
            "infrastructure", "serverless", "cloud-native", "deployment",
            "orchestration",
        ],
        "topics_sub": [],
        "kw_wb": [
            "devops", "kubernetes", "k8s", "docker", "container", "containers",
            "terraform", "ansible", "helm", "gitops", "serverless",
            "observability", "monitoring", "autoscaling",
        ],
        "kw_sub": [
            "ci/cd", "cloud native", "cloud-native", "infrastructure as code",
            "container orchestration", "kubernetes clusters",
        ],
        "langs": [],
    },
    {
        "id": "lang-compilers",
        "label": "Languages/Compilers",
        "emoji": "⚙️",
        "topics": [
            "compiler", "compilers", "interpreter", "programming-language",
            "language", "parser", "lexer", "llvm", "wasm", "webassembly",
            "bytecode", "jit", "transpiler", "type-system", "runtime", "ast",
            "toolchain", "garbage-collection", "language-server",
        ],
        "topics_sub": [],
        "kw_wb": [
            "compiler", "compilers", "interpreter", "parser", "lexer", "llvm",
            "wasm", "webassembly", "bytecode", "jit", "transpiler", "runtime",
            "toolchain",
        ],
        "kw_sub": [
            "programming language", "type system", "garbage collection",
            "abstract syntax tree", "language server", "language and toolchain",
        ],
        "langs": ["zig", "rust", "c", "c++", "ocaml", "haskell"],
    },
    {
        "id": "mobile",
        "label": "Mobile",
        "emoji": "📱",
        "topics": [
            "mobile", "ios", "android", "swiftui", "flutter", "react-native",
            "mobile-app", "xcode", "jetpack-compose", "objective-c", "dart",
        ],
        "topics_sub": [],
        "kw_wb": ["mobile", "ios", "android", "swiftui", "flutter", "xcode"],
        "kw_sub": [
            "react native", "mobile app", "jetpack compose", "app store",
        ],
        "langs": ["swift", "kotlin", "dart", "objective-c"],
    },
]

# Fallback category — used only when nothing else survives.
_GENERAL = {"id": "general", "label": "General", "emoji": "📦"}

# Priority order (index 0 wins ties). General is pinned last.
_PRIORITY: list[str] = [
    "ai-agents", "context-rag", "llm-infra", "ui-ux", "security", "databases",
    "mobile", "lang-compilers", "web-frontend", "devops-cloud", "backend-infra",
    "data-ml", "dev-tools",
]
_PRIORITY_INDEX: dict[str, int] = {cid: i for i, cid in enumerate(_PRIORITY)}
_PRIORITY_INDEX["general"] = len(_PRIORITY)

def priority_index(cid: str) -> int:
    """Priority rank for a category id (0 wins ties); unknown/general sort last."""
    return _PRIORITY_INDEX.get(cid, len(_PRIORITY) + 1)


_BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in _CATEGORIES}
_BY_ID["general"] = _GENERAL

# Public lookup tables.
LABELS: dict[str, str] = {c["id"]: c["label"] for c in _CATEGORIES}
LABELS["general"] = _GENERAL["label"]
EMOJI: dict[str, str] = {c["id"]: c["emoji"] for c in _CATEGORIES}
EMOJI["general"] = _GENERAL["emoji"]

# Precompiled word-boundary regexes, keyed by category id -> {term: pattern}.
_WB_RE: dict[str, dict[str, re.Pattern[str]]] = {
    c["id"]: {t: re.compile(r"\b" + re.escape(t) + r"\b") for t in c["kw_wb"]}
    for c in _CATEGORIES
}

_TOPIC_CAP = 6
_KW_CAP = 4


def _norm_topic(t: Any) -> str:
    return str(t or "").strip().lower()


def _score_category(
    cat: dict[str, Any],
    topics: list[str],
    text: str,
) -> tuple[int, bool]:
    """Return (score, has_topic_or_kw_hit) for one category, before the language hint."""
    cid = cat["id"]

    topic_score = 0
    exact = set(cat["topics"])
    subs = cat["topics_sub"]
    for t in topics:
        if t in exact:
            topic_score += 3
        else:
            for pat in subs:
                if pat in t:
                    topic_score += 3
                    break
    topic_score = min(topic_score, _TOPIC_CAP)

    kw_score = 0
    for term, rx in _WB_RE[cid].items():
        if rx.search(text):
            kw_score += 2
    for term in cat["kw_sub"]:
        if term in text:
            kw_score += 2
    kw_score = min(kw_score, _KW_CAP)

    has_hit = topic_score > 0 or kw_score > 0
    return topic_score + kw_score, has_hit


def classify(repo: dict[str, Any]) -> list[str]:
    """Classify a repo dict into 1..3 category ids. Deterministic and offline.

    Reads ``topics`` (list), ``description``, ``full_name``/``name`` and
    ``language`` from the dict; any of them may be missing. Never mutates input.
    """
    topics = [_norm_topic(t) for t in (repo.get("topics") or [])]
    name = str(repo.get("full_name") or repo.get("name") or "")
    desc = str(repo.get("description") or "")
    text = (name + " " + desc).lower()
    language = _norm_topic(repo.get("language"))

    scored: list[tuple[str, int]] = []
    for cat in _CATEGORIES:
        base, has_hit = _score_category(cat, topics, text)
        if has_hit and language and language in cat["langs"]:
            base += 1
        if base >= 3:
            scored.append((cat["id"], base))

    if not scored:
        return ["general"]

    score_by_id = dict(scored)
    scored.sort(key=lambda p: (-p[1], _PRIORITY_INDEX[p[0]]))
    kept = [cid for cid, _ in scored[:3]]

    # --- suppression rules -------------------------------------------------
    # Drop web-frontend if ui-ux scored >= it.
    if "web-frontend" in kept and "ui-ux" in score_by_id:
        if score_by_id["ui-ux"] >= score_by_id["web-frontend"]:
            kept.remove("web-frontend")
    # Drop data-ml if llm-infra / context-rag / ai-agents scored strictly higher.
    if "data-ml" in kept:
        dm = score_by_id["data-ml"]
        if any(score_by_id.get(o, 0) > dm for o in ("llm-infra", "context-rag", "ai-agents")):
            kept.remove("data-ml")
    # Drop dev-tools if 3 others already qualified.
    if "dev-tools" in kept and len([c for c in kept if c != "dev-tools"]) >= 3:
        kept.remove("dev-tools")

    if not kept:
        return ["general"]
    return kept


def tag_meta(tag_ids: list[str]) -> list[tuple[str, str, str]]:
    """Return ``[(id, label, emoji), ...]`` for a list of category ids."""
    out: list[tuple[str, str, str]] = []
    for cid in tag_ids:
        out.append((cid, LABELS.get(cid, cid), EMOJI.get(cid, "📦")))
    return out


def meta_for(repo: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Convenience: classify ``repo`` then return its ``[(id,label,emoji), ...]``."""
    return tag_meta(classify(repo))
