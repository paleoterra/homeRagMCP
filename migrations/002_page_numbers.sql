-- Add page_number to chunks so PDF results can cite the source page
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS page_number INTEGER;
