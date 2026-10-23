import json
from pathlib import Path

import click

from .pipeline import CodeGeneratorConfig, MergeStrategy, PipelineGenerator


@click.command()
@click.option("--name", "-n", default=None, type=str)
@click.option("--config", "-c", default=None, type=click.Path(exists=True, resolve_path=True))
@click.option("--language", "-l", default="cs", type=click.Choice(["cs", "python", "swift", "cpp"]))
@click.option(
    "--add-validation",
    is_flag=True,
    default=False,
    help="Add runtime validation in __post_init__ (Python) or constructor (C#)",
)
@click.option(
    "--merge-strategy",
    type=click.Choice(["error", "merge", "delete"]),
    default=None,
    help="Strategy for existing value members not in generated code: error (default, raise), merge (keep), delete (remove)",
)
@click.argument("path", default=None, type=click.Path(exists=True, resolve_path=True))
@click.argument("output", default=None, type=click.Path(resolve_path=True))
def json_schema_to_code(name, config, language, add_validation, merge_strategy, path, output):
    with open(path) as f:
        schema = json.load(f)

    if config is not None:
        with open(config) as f:
            config = json.load(f)
            config = CodeGeneratorConfig.from_dict(config)
    else:
        config = CodeGeneratorConfig()

    if add_validation:
        config.add_validation = True

    if merge_strategy is not None:
        config.output.merge_strategy = MergeStrategy(merge_strategy)

    if name is None:
        name = Path(path).stem

    codegen = PipelineGenerator(name, schema, config, language)
    codegen.generate_to_file(Path(output))
