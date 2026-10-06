import pymssql, os, json

c = pymssql.connect(os.getenv('AZURE_SQL_SERVER'), os.getenv('AZURE_SQL_USER'),
    os.getenv('AZURE_SQL_PASSWORD'), os.getenv('AZURE_SQL_DATABASE'))
r = c.cursor()
r.execute('SELECT uid,email_subject,scraped_text,ocr_text,extracted_json,resolved_msa,web_avg_rent_monthly FROM raw_deal_emails WHERE pre_gated=0 ORDER BY uid')
lines = []
for row in r.fetchall():
    uid, subj, sc, ocr, ej, msa, wr = row
    lines.append('=' * 80)
    lines.append(f'UID={uid} | {subj}')
    lines.append('--- SCRAPED ---')
    lines.append(sc or '(empty)')
    lines.append('--- OCR ---')
    lines.append(ocr or '(none)')
    lines.append('--- JSON ---')
    lines.append(json.dumps(json.loads(ej or '{}'), indent=2, default=str))
    lines.append(f'--- msa={msa} | web_rent={wr} ---\n')
c.close()
out = '\n'.join(lines)
open('python_output_dump.txt', 'w').write(out)
print(f'Done — {len(out)} chars written to python_output_dump.txt')
