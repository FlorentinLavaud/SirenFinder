# TODO — SirenFinder & JobIA Pipeline

## 0. Contexte

La résolution SIREN pour JOCAS échoue sur certains cas de correspondance de nom, notamment les sigles comme CCPLM correspondant à COMMUNAUTE DE COMMUNES PIEGE-LAURAGAIS-MALEPERE.

Ce document regroupe les tâches restantes, leur priorité, les actions déjà réalisées et les contraintes d'architecture.

## 1. Déjà réalisé

- duckdb_repository.py : lookup_candidates fusionné en un seul scan parquet avec priorité sigle exact > zip+ville > zip > département et tri jaro_winkler_similarity.
- Correction CAST(code_postal AS VARCHAR).
- SirenCandidate : ajout du champ sigle.
- fetch_sirene.py : stockage de sigleUniteLegale dans une colonne sigle.
- fetch_stock_sirene.py : détection automatique de la colonne sigle INSEE.
- local_ml_siren.py : ajout de query.raw_name dans candidate_scope().
- Correction rapidfuzz vers Levenshtein.normalized_similarity.

## 2. Priorité 1 — Normalisation canonique

Fichier : src/siren_resolver/text_utils.py

Actions :
- Ajouter strip_accents().
- Normaliser les formes juridiques abrégées (SARL, SAS, EURL, SCI, Sté, Ets, Cie, CC, CA, CU, SIVU, SIVOM).
- Créer canonical_name() appliquant lowercase, suppression accents, normalisation juridique, nettoyage ponctuation.
- Remplacer normalize_company_name.
- Ajouter tests accents, casse, ponctuation et formes juridiques.

## 3. Priorité 2 — Matching acronyme

Ajouter looks_like_acronym() et generate_acronym().

Décider entre :
- calcul SQL dans lookup_candidates ;
- provider Python acronym_siren.py exécuté avant le ML.

Les acronymes doivent devenir un signal prioritaire.

Tests : CH de Blois / Centre Hospitalier de Blois.

Il faut aussi récupérer les noms commerciaux des entreprises

SIRENE : LAROCHE TOUEILLE (LAROCHE-TOUEILLE - WELDOM) ; JOCAS donne WELDOM PRADES

## 4. Priorité 3 — Confiance de résolution

Étendre MatchConfidence avec :
- SIGLE_EXACT
- NAME_EXACT_NORMALIZED
- ACRONYM_GENERATED
- FUZZY_HIGH
- ML_ARBITRATED

Propager la vraie confiance jusqu'à ResolutionResult.

Vérifier run_jocas.py et diagnostics.

## 5. Priorité 4 — Régénération base SIRENE - FAIT -

Relancer fetch_sirene.py et/ou fetch_stock_sirene.py.

Vérifier discover_columns() et les logs.

Valider CCPLM après régénération.

## 6. Priorité 5 — Logging et boucle de rétroaction

- Logger les résolutions NONE.
- Logger les désaccords fuzzy vs ML.
- Échantillonner les ML_ARBITRATED proches du strict_threshold pour revue et réentraînement LightGBM.

## 7. Priorité 6 — Tests

Créer un jeu de non-régression :
- CCPLM.
- formes juridiques.
- accents.
- acronymes non déclarés.

Comparer avant/après sur ParquetSirenCache.

## 8. Optimisation performances S3

Constat :
Les appels S3 DuckDB ligne par ligne créent trop de latence.

Actions possibles :
- batch par département ;
- cache local temporaire des partitions.

## 9. Industrialisation Dagster / dbt

Packager SirenProvider et resolver comme composant modulaire Dagster.

Objectif :
intégration entre ingestion offres et transformations dbt.

## 10. Dockerisation Dagster

Créer un Dockerfile embarquant Dagster, DuckDB et le resolver.

## Notes d'architecture

- Conserver le Chain of Responsibility de resolver.py.
- lookup_candidates doit rester un scan parquet unique.
- Toute normalisation doit être identique côté JOCAS et côté SIRENE.