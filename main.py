import asyncio
import json

from workflow import run_project


def display(value):
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json(indent=2)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


async def main():
    request = """
    Create a shipment planning page.

    The planner should select delivery orders and build shipments.

    A shipment cannot exceed vehicle capacity.
    Orders from the same delivery location should be kept together.
    """

    result = await run_project(
        request,
        requirement_code="REQ-CLI-SHIPMENT",
        project_name="Shipment Planning",
    )

    print("\n\n========================================")
    print("SPECIFICATION")
    print("========================================\n")
    print(display(result["specification"]))

    print("\n\n========================================")
    print("UI/UX DESIGN")
    print("========================================\n")
    print(display(result["ui_design"]))

    print("\n\n========================================")
    print("FRONTEND RESULT")
    print("========================================\n")
    print(display(result["frontend"]))

    print("\n\n========================================")
    print("BACKEND RESULT")
    print("========================================\n")
    print(display(result["backend"]))

    print("\n\n========================================")
    print("TEST REPORT")
    print("========================================\n")
    print(display(result["test_report"]))


if __name__ == "__main__":
    asyncio.run(main())
