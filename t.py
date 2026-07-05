import os
import duckdb
import pandas as pd

# 1. Connexion et configuration S3
con = duckdb.connect()
con.execute("LOAD httpfs;")
con.execute(f"SET s3_access_key_id='{os.environ.get('AWS_ACCESS_KEY_ID')}';")
con.execute(f"SET s3_secret_access_key='{os.environ.get('AWS_SECRET_ACCESS_KEY')}';")
if os.environ.get('AWS_SESSION_TOKEN'):
    con.execute(f"SET s3_session_token='{os.environ.get('AWS_SESSION_TOKEN')}';")
    
s3_endpoint = os.environ.get('AWS_S3_ENDPOINT', 'minio.lab.sspcloud.fr')
con.execute(f"SET s3_endpoint='{s3_endpoint}';")
con.execute("SET s3_url_style='path';")

# 2. Chemins S3
s3_path = "s3://flavaud/SirenFinder/jocas_enriched.parquet"
s3_excel_path = "s3://flavaud/SirenFinder/jocas_enriched.xlsx"

# 3. Interroger le fichier directement sur S3 (sans chargement en RAM)
print("--- 5 premières lignes sur S3 ---")
con.sql(f"SELECT * FROM '{s3_path}' LIMIT 5").show()

print("\n--- Nombre de SIREN trouvés ---")
con.sql(f"SELECT COUNT(*) FROM '{s3_path}' WHERE entreprise_siren IS NOT NULL AND entreprise_siren != 'nan'").show()

# --- MODIFICATION : EXTRACTION ALÉATOIRE SÉCURISÉE ---
print("\nExtraction d'un échantillon aléatoire de 500 lignes via DuckDB...")

# 1. On filtre les SIREN vides/nuls
# 2. On utilise 'USING SAMPLE 500 ROWS' pour un vrai tirage aléatoire ultra-rapide
query = f"""
    SELECT * 
    FROM '{s3_path}' 
    WHERE entreprise_siren IS NOT NULL 
      AND entreprise_siren != '' 
      AND entreprise_siren != 'nan'
    USING SAMPLE 500 ROWS;
"""

df_fragment = con.sql(query).to_df()

print(f"Écriture du fichier Excel directement sur S3 : {s3_excel_path}")
storage_options = {
    "key": os.environ.get("AWS_ACCESS_KEY_ID"),
    "secret": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    "token": os.environ.get("AWS_SESSION_TOKEN"),
    "client_kwargs": {"endpoint_url": f"https://{s3_endpoint}"}
}

# Plus besoin de slice [100:500], le dataframe contient exactement tes 500 lignes random
df_fragment.to_excel(
    s3_excel_path, 
    index=False, 
    sheet_name="Jocas Enriched",
    storage_options=storage_options
)
print("Sauvegarde Excel réussie !")