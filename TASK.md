# TODO - SirenFinder & JobIA Pipeline

- [ ] **Bascule du Resolver Local vers S3 (MinIO)**
  * *Constat :* La logique d'upload de la base SIRENE partitionnée sur S3 (MinIO) est en place, mais le resolver et le reste du repo (pipeline) pointent encore sur les fichiers Parquet locaux dans `jocas_siren_work/`.
  * *Action :* Basculer le resolver pour qu'il utilise directement l'arborescence partitionnée par département sur S3.
  * *Détails techniques :* 
    - Intégrer l'extension `httpfs` de DuckDB et injecter les variables d'environnement Onyxia (`AWS_ACCESS_KEY_ID`, etc.).
    - Dynamiser le chemin dans le `read_parquet()` selon le département de l'offre (ex: `s3://bucket/sirene/departement={dept}/*.parquet`) pour activer le blocking spatial directement sur le cloud.

- [ ] **Robustesse et Data Quality du Partitionnement Géographique**
  * *Constat :* Si une offre possède un code département mal formaté (`"41"` au lieu de `"041"`, `None`, ou cas spécifiques comme la Corse `2A`/`2B`), la requête S3 dynamique lèvera une erreur de chemin introuvable.
  * *Action :* Créer un helper de normalisation stricte des codes géographiques avant l'injection dans le chemin S3.
  * *Fallback :* Prévoir une stratégie dégradée (ex: si département introuvable ou invalide, basculer sur un scan d'une table de secours ou journaliser l'erreur proprement sans bloquer le pipeline).

- [ ] **Optimisation des Performances S3 (Smart-Scanning / Batching)**
  * *Constat :* Interroger S3 en HTTP via DuckDB pour chaque entreprise individuellement (ligne par ligne) va générer une latence réseau massive et effondrer les performances.
  * *Action :* Optimiser la stratégie de requêtage.
  * *Pistes :* 
    - Batcher les résolutions par département (regrouper toutes les entreprises du "41" pour n'ouvrir la partition S3 qu'une seule fois).
    - Ou évaluer un système de cache local éphémère (télécharger la partition du département concerné sur le disque local de l'instance le temps du traitement, puis la purger).

- [ ] **Industrialisation et Intégration dans l'Orchestrateur (Dagster / dbt)**
  * *Constat :* Le resolver doit s'intégrer proprement dans la future architecture cible de traitement de données de JobIA.
  * *Action :* Packager la logique du `SirenProvider` et du resolver sous forme d'un composant modulaire (ex: une Software-Defined Asset ou une Op Dagster).
  * *Objectif :* Permettre une intégration fluide en aval de l'ingestion des offres et en amont des transformations lourdes sur dbt.