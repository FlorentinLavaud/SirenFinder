- [ ] **Migration S3 / Optimisation du Resolver SIRENE**
  * *Constat :* La logique d'upload de la base SIRENE partitionnée sur S3 (MinIO) est en place, mais le resolver et le reste du repo (pipeline) pointent encore sur les fichiers Parquet locaux dans `jocas_siren_work/`.
  * *Action :* Basculer le resolver pour qu'il utilise directement l'arborescence partitionnée par département sur S3.
  * *Détails techniques :* 
    - Intégrer l'extension `httpfs` de DuckDB et injecter les variables d'environnement Onyxia (`AWS_ACCESS_KEY_ID`, etc.).
    - Dynamiser le chemin dans le `read_parquet()` selon le département de l'offre (ex: `s3://bucket/sirene/departement={dept}/*.parquet`) pour activer le blocking spatial directement sur le cloud.