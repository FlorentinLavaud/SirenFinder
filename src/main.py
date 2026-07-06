"""Point d'entrée : résout les SIREN manquants pour contracting et awarded.

Usage :
    python main.py

Variables d'environnement attendues (voir siren_resolver/config.py) :
    SIREN_DATA_DIR                   dossier contenant les parquet en entrée/sortie
    SIREN_REFERENCE_ROOT             chemin vers le référentiel SIRENE nettoyé (parquet partitionné)
    LOCAL_ML_MODEL_PATH              chemin vers le modèle LightGBM (optionnel)
    LOCAL_ML_BI_ENCODER_MODEL_NAME   nom du modèle sentence-transformers
    LOCAL_ML_CANDIDATE_LIMIT         nombre max de candidats DuckDB
    LOCAL_ML_STRICT_THRESHOLD        seuil de validation final
"""
from __future__ import annotations

import logging

from siren_resolver import (
    AWARDED_COLUMNS,
    CONTRACTING_COLUMNS,
    CacheSirenProvider,
    LocalMlSirenConfig,
    LocalMlSirenProvider,
    ParquetSirenCache,
    ResolverConfig,
    SirenResolutionPipeline,
    SirenResolver,
)


def build_pipeline(config: ResolverConfig, columns, stock_filename: str) -> SirenResolutionPipeline:
    cache = ParquetSirenCache(
        path=config.pipeline.data_dir / stock_filename,
        name_col=columns.name_col,
        address_col=columns.address_col,
        siren_col=columns.siren_col,
        prune_probability=config.pipeline.cache_prune_probability,
        prune_seed=config.pipeline.cache_prune_seed,
    )
    providers = [
        CacheSirenProvider(cache),
        LocalMlSirenProvider(
            LocalMlSirenConfig(
                parquet_root=config.pipeline.siren_reference_root,
                model_path=config.pipeline.local_ml_model_path,
                bi_encoder_model_name=config.pipeline.local_ml_bi_encoder_model_name,
                candidate_limit=config.pipeline.local_ml_candidate_limit,
                strict_threshold=config.pipeline.local_ml_strict_threshold,
            )
        ),
    ]
    resolver = SirenResolver(cache=cache, providers=providers)
    return SirenResolutionPipeline(config=config, resolver=resolver, cache=cache, columns=columns)


def main() -> None:
    config = ResolverConfig()
    logging.basicConfig(
        level=config.pipeline.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    data_dir = config.pipeline.data_dir

    contracting_pipeline = build_pipeline(config, CONTRACTING_COLUMNS, "stock_contracting_sirens.parquet")
    contracting_pipeline.run(
        missing_input_path=data_dir / "contracting_missing_siren.parquet",
        resolved_output_path=data_dir / "estimated_sirens_contracting.parquet",
    )

    awarded_pipeline = build_pipeline(config, AWARDED_COLUMNS, "stock_awarded_sirens.parquet")
    awarded_pipeline.run(
        missing_input_path=data_dir / "awarded_missing_siren.parquet",
        resolved_output_path=data_dir / "estimated_sirens_awarded.parquet",
    )


if __name__ == "__main__":
    main()
