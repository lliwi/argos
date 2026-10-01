"""Canal CLI/TUI mínimo (RF-06): muestra progreso y resuelve aprobaciones en la terminal.

Es un adaptador fino (P1): solo traduce eventos de progreso a salida y respuestas humanas a
decisiones; toda la lógica vive en el núcleo.
"""

from __future__ import annotations

from rich.console import Console

from argos.governance.approval import CliApprover

console = Console()


def progress_printer(verbose: bool = True):
    def on_progress(kind: str, text: str) -> None:
        if kind == "thinking":
            console.print(f"[dim]… {text}[/]")
        elif kind == "tool":
            console.print(f"[cyan]⚙ {text[:300]}[/]")
        elif kind == "observation" and verbose:
            clipped = text if len(text) < 800 else text[:800] + " …"
            console.print(f"[dim]{clipped}[/]", highlight=False, markup=False)
        elif kind == "final":
            console.print(f"[bold green]✔ {text}[/]")

    return on_progress


def cli_approver() -> CliApprover:
    return CliApprover(console)
