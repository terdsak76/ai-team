import asyncio

from agents import Runner

from team_agents.specification import specification_agent
from team_agents.frontend import frontend_agent
from team_agents.backend import backend_agent


async def main():

    request = """
    Create a shipment planning page.

    The planner should select delivery orders and build shipments.

    A shipment cannot exceed vehicle capacity.
    Orders from the same delivery location should be kept together.
    """

    # ==========================================================
    # 1. Create specification
    # ==========================================================

    print("\n=== 1. GENERATING SPECIFICATION ===\n")

    spec_result = await Runner.run(
        specification_agent,
        request,
    )

    spec = spec_result.final_output

    print(spec.model_dump_json(indent=2))

    # Convert Pydantic model -> JSON string
    # so frontend/backend receive exactly the same specification.
    spec_json = spec.model_dump_json(indent=2)

    # ==========================================================
    # 2. Prepare prompts for frontend/backend
    # ==========================================================

    frontend_prompt = f"""
You are responsible for implementing the FRONTEND portion
of the following approved software specification.

Do not change the API contract.

Do not implement backend code.

Produce:

1. Proposed frontend architecture
2. Pages/components required
3. TypeScript interfaces
4. API integration design
5. Validation rules
6. Loading/error/empty states
7. Implementation code
8. Frontend tests that should be created

APPROVED SPECIFICATION:

{spec_json}
"""

    backend_prompt = f"""
You are responsible for implementing the BACKEND portion
of the following approved software specification.

Do not change frontend requirements.

Do not invent endpoints that are not required by the specification.

Produce:

1. Proposed backend architecture
2. API routes
3. Pydantic models
4. Service layer design
5. Database interaction
6. Business-rule validation
7. Error handling
8. Implementation code
9. Backend tests that should be created

APPROVED SPECIFICATION:

{spec_json}
"""

    # ==========================================================
    # 3. Run frontend and backend in parallel
    # ==========================================================

    print("\n=== 2. STARTING FRONTEND + BACKEND ===\n")

    frontend_result, backend_result = await asyncio.gather(

        Runner.run(
            frontend_agent,
            frontend_prompt,
        ),

        Runner.run(
            backend_agent,
            backend_prompt,
        ),
    )

    # ==========================================================
    # 4. Results
    # ==========================================================

    print("\n\n========================================")
    print("FRONTEND RESULT")
    print("========================================\n")

    print(frontend_result.final_output)

    print("\n\n========================================")
    print("BACKEND RESULT")
    print("========================================\n")

    print(backend_result.final_output)


if __name__ == "__main__":
    asyncio.run(main())