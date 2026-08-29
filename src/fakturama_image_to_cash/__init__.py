import os
import sys


def main() -> None:
    """Run the full Order-image -> verified Invoice workflow against a running Fakturama.

    Usage: fakturama-image-to-cash [path/to/order-image.png]
    Defaults to the bundled sample image if no path is given.
    """
    import anthropic

    from .workflow import run_workflow

    image_path = sys.argv[1] if len(sys.argv) > 1 else "screenshots/01-source-order.png"

    # A standard workspace-scoped API key needs nothing extra. An identity-linked key
    # (tied to a personal login rather than a workspace) additionally requires the
    # workspace it should act in - confirmed live against this exact account type.
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace_id}) if workspace_id else None

    order_title, invoice_title = run_workflow(image_path, client=client)
    print(f"Order saved as {order_title}, linked Invoice saved as {invoice_title}")
