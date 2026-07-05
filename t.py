import duckdb

# Remplacez par le chemin exact d'un de vos fichiers Parquet existants
parquet_file = "/home/onyxia/work/SirenFinder/jocas_siren_work/jocas_resolved_siren.parquet"

# Connectez-vous (en mémoirse) et lisez le fichier
con = duckdb.connect()

# 1. Voir le schéma (les colonnes et leurs types)
print("--- Schéma du fichier ---")
con.sql(f"DESCRIBE SELECT * FROM '{parquet_file}'").show()

# 2. Voir les 5 premières lignes
print("\n--- 5 premières lignes ---")
con.sql(f"SELECT * FROM '{parquet_file}' LIMIT 5").show()

# 3. voir où le siren a été trouver 
print("\n--- 10 lignes avec siren ---")
# Afficher les lignes qui n'ont pas un SIREN égal à 'nan' ou NULL
con.sql(f"SELECT * FROM '{parquet_file}' WHERE entreprise_siren IS NOT NULL AND entreprise_siren != 'nan' LIMIT 10").show()