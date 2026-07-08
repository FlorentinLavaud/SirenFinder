# TODO - SirenFinder & JobIA Pipeline

TODO — Amélioration du matching SIREN (SirenFinder)

Contexte : la résolution SIREN pour JOCAS échoue sur certains cas de
correspondance de nom (ex. sigle "CCPLM" pour "COMMUNAUTE DE COMMUNES
PIEGE-LAURAGAIS-MALEPERE") que ni le SQL de récupération de candidats ni le
modèle ML en aval ne traitent correctement. Ce document liste les tâches
restantes, dans l'ordre de priorité recommandé.


✅ Déjà fait (session précédente)


 duckdb_repository.py : lookup_candidates fusionné en un seul scan
parquet (au lieu de 3 requêtes séquentielles), avec colonne priority
(sigle exact > zip+ville > zip > département) et tri par
jaro_winkler_similarity sur GREATEST(raison_sociale, enseigne, sigle).
 Fix CAST(code_postal AS VARCHAR) pour éviter le crash de conversion
sur les codes postaux non numériques / non français.
 SirenCandidate : ajout du champ sigle.
 fetch_sirene.py : sigleUniteLegale stocké comme colonne sigle
propre au lieu d'un simple fallback de raison_sociale.
 fetch_stock_sirene.py : détection auto de la colonne sigle dans le
fichier stock INSEE en masse (choose_column), propagée dans le
parquet de sortie.
 local_ml_siren.py : candidate_scope() reçoit query.raw_name pour
alimenter le matching sigle/nom côté SQL.
 Fix rapidfuzz.fuzz.normalized_levenshtein (n'existe pas) →
rapidfuzz.distance.Levenshtein.normalized_similarity.



🔲 1. Normalisation canonique — PRIORITAIRE (fondation de tout le reste)

Fichier : src/siren_resolver/text_utils.py


 Ajouter strip_accents(text: str) -> str (via unicodedata.normalize("NFKD", ...)
+ suppression des marques diacritiques).
 Construire un dictionnaire de formes juridiques abrégées à normaliser
ou supprimer avant comparaison : SARL, SAS, EURL, SCI, Sté →
société, Ets → établissements, Cie → compagnie, sigles EPCI
courants (CC, CA, CU, SIVU, SIVOM), etc.
 Écrire une fonction unique canonical_name(name: str) -> str qui
applique : lowercase → strip accents → normalisation formes
juridiques → collapse espaces/ponctuation. Doit être la seule
fonction de normalisation utilisée, côté query JOCAS ET côté
candidats SIRENE (pour éviter que deux chemins de code divergent).
 Remplacer les usages actuels de normalize_company_name par
canonical_name là où c'est pertinent (vérifier tous les appelants).
 Ajouter des tests unitaires : variantes accents ("Ecole"/"École"),
casse, formes juridiques abrégées vs longues, ponctuation
(points, tirets, apostrophes).


🔲 2. Détection + matching acronyme généré (cas sans sigle déclaré)

Fichier : src/siren_resolver/text_utils.py (nouvelles fonctions) +
src/siren_resolver/providers/ (nouveau provider ou nouveau tier SQL)


 looks_like_acronym(name: str) -> bool : heuristique (tout
majuscule, sans espace après nettoyage ponctuation, longueur ≤ 8,
alphabétique).
 generate_acronym(name: str, stopwords: set[str] | None = None) -> str :
première lettre de chaque mot significatif (hors stopwords FR : de,
du, des, la, le, les, et, en, sur, à, aux, d', l').
 Décider de l'implémentation : soit (a) une colonne SQL calculée à la
volée dans lookup_candidates comparant l'acronyme généré du
candidat à raw_name si looks_like_acronym(raw_name) est vrai,
soit (b) un provider Python dédié (acronym_siren.py) exécuté tôt
dans la chaîne de resolver.py, avant le provider ML.
 Si looks_like_acronym(query.raw_name) est vrai, ne pas laisser
jaro-winkler/embeddings scorer le nom complet comme signal principal
(bruit) — prioriser le match acronyme booléen.
 Tests sur des cas type "CH de Blois" / "Centre Hospitalier de Blois".


🔲 3. Granularité de la confiance de résolution

Fichiers : src/siren_resolver/models.py, src/siren_resolver/resolver.py


 Étendre MatchConfidence (actuellement CACHE / OFFICIAL_API /
GOOGLE_CSE / NONE) avec des niveaux plus fins : SIGLE_EXACT,
NAME_EXACT_NORMALIZED, ACRONYM_GENERATED, FUZZY_HIGH,
ML_ARBITRATED.
 Propager cette confiance depuis chaque tier/provider jusqu'à
ResolutionResult (actuellement _resolve_single tague tout
résultat non-cache en OFFICIAL_API, quel que soit le tier réel —
perte d'information à corriger).
 Vérifier tous les consommateurs de MatchConfidence (run_jocas.py,
diagnostics) pour s'assurer qu'ils gèrent les nouvelles valeurs sans
casser un éventuel match/case ou comparaison exhaustive.


🔲 4. Re-génération de la base de référence SIRENE


 Relancer fetch_sirene.py et/ou fetch_stock_sirene.py pour
peupler réellement la colonne sigle sur le parquet S3/MinIO —
tant que ce n'est pas fait, sigle est vide partout et le patch
SQL n'a aucun effet.
 Vérifier via discover_columns() / les logs que le fichier stock
INSEE utilisé contient bien une colonne sigle exploitable (nom exact
à confirmer selon la version d'export).
 Valider sur le cas CCPLM une fois la base régénérée.


🔲 5. Boucle de rétroaction / logging


 Logger explicitement les résolutions NONE (aucun candidat au-dessus
du seuil) plutôt que de forcer un match à tout prix.
 Logger les désaccords entre tiers (ex. le meilleur candidat fuzzy
diffère du meilleur candidat ML) — cas les plus instructifs pour
calibrer les seuils.
 Mettre en place un échantillonnage périodique des résolutions
ML_ARBITRATED proches du seuil (strict_threshold) pour revue
manuelle et ajustement / ré-entraînement du LightGBM.


🔲 6. Tests / non-régression


 Jeu de test dédié avec les cas connus : sigle (CCPLM), formes
juridiques abrégées, accents, acronymes non déclarés.
 Comparer les résolutions avant/après sur le cache existant
(ParquetSirenCache) pour détecter toute régression introduite par
les changements de priorité SQL ou de normalisation.



Notes d'architecture à respecter


Le pattern Chain of Responsibility de resolver.py (premier provider
suffisamment fiable gagne) doit être conservé ; toute nouvelle couche de
matching (acronyme, normalisation) s'intègre dans ce flux plutôt que de
le contourner.
lookup_candidates doit rester un scan unique du parquet par appel —
ne pas réintroduire de requêtes séquentielles multiples par tier.
Toute nouvelle fonction de normalisation doit être appliquée de façon
identique côté query (JOCAS) et côté référentiel (SIRENE) pour éviter
une dérive entre les deux représentations.


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

- [ ] **Dockerisation du Pipeline Dagster**
  * *Action :* Créer un `Dockerfile` pour le projet afin d'embarquer Dagster, DuckDB et ton resolver dans une image unique, prête à être déployée sur n'importe quel serveur (VPS, AWS ECS, etc.).