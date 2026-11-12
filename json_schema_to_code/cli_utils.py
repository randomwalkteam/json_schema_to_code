"""
CLI utilities for command line reconstruction and introspection.
"""

from pathlib import Path

import click


def reconstruct_command_line(click_command: click.Command) -> str:
    """
    Reconstruct command line from current Click context using introspection.

    Args:
        click_command: Click command object for introspection

    Returns:
        Reconstructed command line string
    """
    # Try to get current Click context for parameter values
    try:
        ctx = click.get_current_context()
        cli_args = ctx.params
    except RuntimeError:
        # No active context, return basic command
        return "json_schema_to_code"

    if not cli_args:
        return "json_schema_to_code"

    cmd_parts = ["json_schema_to_code"]

    # Get Click command metadata
    arguments = []  # For positional arguments
    options = []  # For optional arguments

    for param in click_command.params:
        param_name = param.name
        if param_name not in cli_args:
            continue

        value = cli_args[param_name]
        if not value:
            continue

        # A file path shows as its file name: the header must not depend on where the
        # generator ran, nor on whether the output file existed before this run.
        if isinstance(param.type, click.Path):
            formatted_value = Path(str(value)).name
        else:
            formatted_value = str(value)

        if isinstance(param, click.Argument):
            # Handle positional arguments
            arguments.append(formatted_value)

        elif isinstance(param, click.Option):
            # Handle optional arguments
            # Skip if it's the default value
            if hasattr(param, "default") and value == param.default:
                continue

            # Get the primary option name (first in opts list)
            flag = param.opts[0] if param.opts else f"--{param_name}"
            options.extend([flag, formatted_value])

    # Combine: command + arguments + options
    cmd_parts.extend(arguments)
    cmd_parts.extend(options)

    return " ".join(cmd_parts)
