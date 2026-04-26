"""
Jarvis Memory System — three-tier storage
  Hot  : small JSON file, always injected into every prompt (~300 tokens max)
  Warm : ChromaDB vector store, semantic search retrieves top-k per query
  Cold : append-only JSONL archive for consolidated/old warm entries

Requires:  pip install chromadb sentence-transformers
Degrades gracefully if those packages are absent.
"""

import json
import os
import threading
import time

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "jarvis_memory")
HOT_FILE   = os.path.join(MEMORY_DIR, "hot.json")
COLD_FILE  = os.path.join(MEMORY_DIR, "cold_archive.jsonl")
CHROMA_DIR = os.path.join(MEMORY_DIR, "chroma")

CONSOLIDATION_THRESHOLD = 400   # archive oldest 100 when warm layer exceeds this
COSINE_DISTANCE_CUTOFF  = 0.65  # discard results less similar than this


class MemoryManager:
    def __init__(self):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        self._hot        = self._load_hot()
        self._lock       = threading.Lock()
        self._collection = None
        self._embedder   = None
        self._ready      = threading.Event()
        threading.Thread(target=self._init_backends, daemon=True).start()

    # ── Initialization ────────────────────────────────────────────────────────

    def _init_backends(self):
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
            self._embedder   = SentenceTransformer("all-MiniLM-L6-v2")
            chroma           = chromadb.PersistentClient(path=CHROMA_DIR)
            self._collection = chroma.get_or_create_collection(
                "jarvis_memory", metadata={"hnsw:space": "cosine"}
            )
            print(f"  Memory system ready ({self._collection.count()} memories stored).")
        except ImportError:
            print("  Memory: install chromadb + sentence-transformers to enable semantic search.")
        except Exception as e:
            print(f"  Memory init error: {e}")
        finally:
            self._ready.set()

    # ── Hot Layer ─────────────────────────────────────────────────────────────

    def _load_hot(self) -> dict:
        if os.path.exists(HOT_FILE):
            try:
                with open(HOT_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"facts": [], "preferences": {}, "people": {}}

    def _save_hot(self):
        try:
            with open(HOT_FILE, "w", encoding="utf-8") as f:
                json.dump(self._hot, f, indent=2)
        except Exception as e:
            print(f"  Hot memory save error: {e}")

    def get_hot_context(self) -> str:
        """Return formatted hot-layer string for prompt injection."""
        parts = []
        if self._hot.get("facts"):
            parts.append("Facts: " + "; ".join(self._hot["facts"][:20]))
        if self._hot.get("preferences"):
            parts.append("Preferences: " + "; ".join(
                f"{k}={v}" for k, v in list(self._hot["preferences"].items())[:10]
            ))
        if self._hot.get("people"):
            parts.append("People: " + "; ".join(
                f"{k}: {v}" for k, v in list(self._hot["people"].items())[:8]
            ))
        return "\n".join(parts)

    def update_hot(self, category: str, key: str, value: str = ""):
        """Write to the always-on hot layer. category: 'fact' | 'preference' | 'person'"""
        with self._lock:
            if category == "fact":
                if key not in self._hot["facts"]:
                    self._hot["facts"].append(key)
                    self._hot["facts"] = self._hot["facts"][-30:]
            elif category == "preference":
                self._hot["preferences"][key] = value
            elif category == "person":
                self._hot["people"][key] = value
            self._save_hot()

    def remove_hot(self, keyword: str):
        """Remove any hot entries containing keyword."""
        kw = keyword.lower()
        with self._lock:
            self._hot["facts"] = [f for f in self._hot["facts"] if kw not in f.lower()]
            for d in (self._hot["preferences"], self._hot["people"]):
                for k in list(d):
                    if kw in k.lower() or kw in str(d[k]).lower():
                        del d[k]
            self._save_hot()

    # ── Warm Layer ────────────────────────────────────────────────────────────

    def add(self, content: str, category: str = "general"):
        """Embed and store a memory in the warm layer."""
        if not self._collection or not self._embedder:
            return
        try:
            vec    = self._embedder.encode(content).tolist()
            doc_id = f"m{int(time.time()*1000)}_{abs(hash(content)) % 99999}"
            self._collection.add(
                ids=[doc_id],
                embeddings=[vec],
                documents=[content],
                metadatas=[{"category": category, "ts": time.time()}],
            )
            if self._collection.count() > CONSOLIDATION_THRESHOLD:
                threading.Thread(target=self._consolidate, daemon=True).start()
        except Exception as e:
            print(f"  Memory add error: {e}")

    def retrieve(self, query: str, top_k: int = 5) -> list:
        """Return the top_k most semantically relevant memories for query."""
        if not self._collection or not self._embedder:
            return []
        n = self._collection.count()
        if n == 0:
            return []
        try:
            vec = self._embedder.encode(query).tolist()
            res = self._collection.query(
                query_embeddings=[vec],
                n_results=min(top_k, n),
                include=["documents", "distances"],
            )
            return [
                doc for doc, dist
                in zip(res["documents"][0], res["distances"][0])
                if dist < COSINE_DISTANCE_CUTOFF
            ]
        except Exception as e:
            print(f"  Memory retrieve error: {e}")
            return []

    def delete_by_keyword(self, keyword: str):
        """Delete warm memories whose text contains keyword."""
        if not self._collection:
            return
        try:
            all_items = self._collection.get(include=["documents"])
            ids_to_del = [
                id_ for id_, doc in zip(all_items["ids"], all_items["documents"])
                if keyword.lower() in doc.lower()
            ]
            if ids_to_del:
                self._collection.delete(ids=ids_to_del)
        except Exception as e:
            print(f"  Memory delete error: {e}")

    # ── Cold Layer / Consolidation ────────────────────────────────────────────

    def _consolidate(self):
        """Archive the oldest 100 warm entries to cold storage."""
        try:
            if self._collection.count() <= CONSOLIDATION_THRESHOLD:
                return
            all_items = self._collection.get(include=["documents", "metadatas"])
            if not all_items["ids"]:
                return
            paired = sorted(
                zip(all_items["ids"], all_items["documents"], all_items["metadatas"]),
                key=lambda x: x[2].get("ts", 0),
            )
            to_archive = paired[:100]
            with open(COLD_FILE, "a", encoding="utf-8") as f:
                for _, doc, meta in to_archive:
                    f.write(json.dumps({"content": doc, "meta": meta}) + "\n")
            self._collection.delete(ids=[x[0] for x in to_archive])
            print(f"  Memory consolidated: archived {len(to_archive)} old entries.")
        except Exception as e:
            print(f"  Consolidation error: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def count(self) -> int:
        return self._collection.count() if self._collection else 0


memory = MemoryManager()
