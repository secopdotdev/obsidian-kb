"""Fixture: click CLI with a single `deploy` command and two options.

Used by test_harvest.py to verify harvest_click_typer extracts 1 item with
command name 'deploy' and flags --env, --force. Not intended to be executed.
"""
import click


@click.command()
@click.option("--env", default="prod", help="Target environment")
@click.option("--force", is_flag=True, help="Force deployment without confirmation")
def deploy(env, force):
    """Deploy the application to the target environment."""
    click.echo(f"Deploying to {env} (force={force})")


if __name__ == "__main__":
    deploy()
