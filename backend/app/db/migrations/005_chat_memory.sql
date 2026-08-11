-- Chat memory: retrieve relevant turns instead of replaying recent ones.
--
-- Replaying the last N turns costs tokens linearly in N on EVERY turn, and most
-- of what it replays is irrelevant to the current question. Retrieval by
-- similarity costs a constant K instead, and the K it returns are the ones that
-- actually bear on the question.
--
-- WHY pgvector AND NOT A GRAPH DATABASE.
--
-- The query this serves is: "prior turns in this session that resemble the
-- current question". That is a vector similarity search with one scalar filter.
-- It is not a traversal — there is no variable-length path to walk, no
-- relationship to hop across. Postgres already has pgvector provisioned, and the
-- filter and the similarity resolve in a single index-assisted query:
--
--     WHERE session_id = $1 ORDER BY embedding <=> $2 LIMIT 6
--
-- Adding Neo4j for this would mean a fifth datastore, a fifth failure mode, a
-- sync job to keep it consistent with Postgres, and a cross-database join on
-- every chat turn — to answer a query one index already answers. Revisit when a
-- genuine multi-hop question appears (cross-basin hazard propagation is the real
-- candidate); chat history is not one.

-- Embedding of each turn's content. Nullable: embeddings are computed
-- best-effort, and a turn with no vector is simply never retrieved rather than
-- blocking the write. Chat must work with no embedding provider configured.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS embedding VECTOR(${EMBEDDING_DIMENSIONS});

-- Token cost of producing an assistant turn, from the provider's usage block.
-- 0 for the deterministic path, which is the point — this column is how you see
-- what the zero-token answers are saving.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS tokens_used INTEGER NOT NULL DEFAULT 0;

-- Which path produced the answer: 'llm' | 'deterministic' | 'cache'.
-- Reported by GET /chat/economics so the deterministic hit rate is observable
-- rather than assumed.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS answered_by TEXT;

-- Retrieval is always scoped to one session, so the index leads with it. A bare
-- HNSW index on `embedding` would search across every subscriber's history and
-- then filter, which is both slower and — far worse — would let another
-- subscriber's turn surface as a candidate before the filter dropped it.
CREATE INDEX IF NOT EXISTS chat_messages_session_embedding_idx
    ON chat_messages (session_id)
    INCLUDE (embedding)
    WHERE embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS chat_messages_answered_by_idx
    ON chat_messages (answered_by, created_at DESC);
