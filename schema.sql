-- ============================================================
--  Gestor Telegram - Schema Supabase
--  Execute no SQL Editor do Supabase (uma vez)
-- ============================================================

-- Tabela de campanhas (dados completos em JSONB)
CREATE TABLE IF NOT EXISTS campaigns (
  id   TEXT PRIMARY KEY,
  data JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Habilita RLS (o service key ignora a RLS automaticamente)
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;

-- ============================================================
--  Storage: bucket de mídia
--  Execute separado ou via Dashboard > Storage > New bucket
-- ============================================================

-- Cria bucket público "media"
INSERT INTO storage.buckets (id, name, public)
VALUES ('media', 'media', true)
ON CONFLICT DO NOTHING;

-- Leitura pública (para o Telegram baixar as imagens)
CREATE POLICY "Leitura publica de midia"
  ON storage.objects FOR SELECT
  USING (bucket_id = 'media');

-- Upload via service key (backend)
CREATE POLICY "Upload via service key"
  ON storage.objects FOR INSERT
  WITH CHECK (bucket_id = 'media');

-- Delete via service key (opcional)
CREATE POLICY "Delete via service key"
  ON storage.objects FOR DELETE
  USING (bucket_id = 'media');
