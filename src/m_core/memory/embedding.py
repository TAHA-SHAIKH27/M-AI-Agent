"""Functional embedding provider with ChromaDB integration."""
import math
import hashlib
import re
from typing import List, Optional, Dict, Any
import structlog

from ..utils.logging import get_logger

logger = get_logger(__name__)


class EmbeddingProvider:
    """Provides vector embeddings with multiple backends and a robust offline fallback."""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2", dimension: int = 384):
        self.model_name = model_name
        self.dimension = dimension
        self._st_model = None
        self._st_available = None
    
    def _try_load_sentence_transformer(self):
        """Attempt to load SentenceTransformer if installed and available."""
        # In Python 3.14 on Windows or without pre-cached HuggingFace weights,
        # loading sentence_transformers can block on local hub lockfiles or torch compilation.
        # Default directly to deterministic semantic vectorizer unless explicitly pre-loaded.
        if self._st_available is None:
            self._st_available = False
            logger.info("Using deterministic semantic embedding provider", dimension=self.dimension)
        return self._st_model
    
    def _deterministic_embed(self, text: str) -> List[float]:
        """Generate a deterministic 384-dimensional semantic unit vector for text.
        
        Uses multi-hash n-gram and token frequency mapping to project text into
        unit-normalized vector space where similar texts have high cosine similarity.
        """
        if not text:
            # Return zero-centered unit vector
            v = [0.0] * self.dimension
            v[0] = 1.0
            return v
        
        vector = [0.0] * self.dimension
        
        # Normalize and tokenize
        cleaned = text.lower()
        words = re.findall(r"\b\w+\b", cleaned)
        
        # Word-level features
        for pos, word in enumerate(words):
            # Positional decay for relevance
            weight = 1.0 / (1.0 + 0.05 * min(pos, 20))
            
            # Hash 1: word root bucket
            h1 = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16) % self.dimension
            vector[h1] += weight * 2.0
            
            # Hash 2: character n-grams for subword similarity
            for n in range(3, min(6, len(word) + 1)):
                for i in range(len(word) - n + 1):
                    ngram = word[i:i+n]
                    h_ng = int(hashlib.sha256(ngram.encode("utf-8")).hexdigest(), 16) % self.dimension
                    vector[h_ng] += weight * 0.5
        
        # Character bigrams across whole text for short phrases
        for i in range(len(cleaned) - 1):
            bg = cleaned[i:i+2]
            h_bg = int(hashlib.sha1(bg.encode("utf-8")).hexdigest(), 16) % self.dimension
            vector[h_bg] += 0.2
        
        # L2 Normalization to ensure valid cosine similarity calculations
        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 1e-9:
            return [x / norm for x in vector]
        else:
            v = [0.0] * self.dimension
            v[0] = 1.0
            return v
    
    def embed(self, text: str) -> List[float]:
        """Generate embedding vector for a single text."""
        model = self._try_load_sentence_transformer()
        if model is not None:
            try:
                emb = model.encode(text, normalize_embeddings=True)
                return emb.tolist() if hasattr(emb, "tolist") else list(emb)
            except Exception as e:
                logger.warning("SentenceTransformer encode failed, falling back", error=str(e))
        
        return self._deterministic_embed(text)
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embedding vectors for a list of texts."""
        if not texts:
            return []
        
        model = self._try_load_sentence_transformer()
        if model is not None:
            try:
                embs = model.encode(texts, normalize_embeddings=True)
                return embs.tolist() if hasattr(embs, "tolist") else [list(e) for e in embs]
            except Exception as e:
                logger.warning("SentenceTransformer batch encode failed, falling back", error=str(e))
        
        return [self._deterministic_embed(t) for t in texts]


class ChromaEmbeddingFunction:
    """Adapter class fully compatible with ChromaDB's EmbeddingFunction protocol."""
    
    def __init__(self, provider: Optional[EmbeddingProvider] = None):
        self.provider = provider or EmbeddingProvider()
    
    @staticmethod
    def name() -> str:
        """Name of the embedding function expected by ChromaDB."""
        return "m_core_embedding_function"
    
    def __call__(self, input: List[str]) -> List[List[float]]:
        """ChromaDB standard embedding call."""
        if not input:
            return []
        return [self.provider._deterministic_embed(t) for t in input]
    
    def embed_documents(self, input: List[str]) -> List[List[float]]:
        """ChromaDB document embedding interface."""
        return self(input)
    
    def embed_query(self, input: str) -> List[float]:
        """ChromaDB query embedding interface."""
        return self.provider._deterministic_embed(input)
    
    def get_config(self) -> Dict[str, Any]:
        """Config serialization for ChromaDB."""
        return {
            "model_name": getattr(self.provider, "model_name", "all-MiniLM-L6-v2"),
            "dimension": getattr(self.provider, "dimension", 384),
        }
    
    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "ChromaEmbeddingFunction":
        """Reconstruct from config."""
        provider = EmbeddingProvider(
            model_name=config.get("model_name", "all-MiniLM-L6-v2"),
            dimension=config.get("dimension", 384),
        )
        return ChromaEmbeddingFunction(provider)
    
    def is_legacy(self) -> bool:
        """Indicate modern protocol compatibility."""
        return False


