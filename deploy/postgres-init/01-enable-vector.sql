-- The configured POSTGRES_USER initializes this isolated demo database and is
-- privileged enough to install pgvector before Omai creates its RAG tables.
CREATE EXTENSION IF NOT EXISTS vector;
