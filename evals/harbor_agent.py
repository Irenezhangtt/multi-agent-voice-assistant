"""Harbor host-side adapter. The application itself runs inside each sandbox."""
from harbor.agents.installed.base import BaseInstalledAgent


class ClinicAgent(BaseInstalledAgent):
    @staticmethod
    def name():
        return "clinic-voice-workflows"

    async def install(self, environment):
        # Dependencies and the frozen app are installed at image build time.
        await self.exec_as_agent(environment, "python -c 'from voice_agent.runtime import AgentRuntime'", timeout_sec=30)

    async def run(self, instruction, environment, context):
        # The task contains scripted caller turns, not instructions to a coding agent.
        await self.exec_as_agent(
            environment,
            "python -m evals.harbor_trial run --case /app/case.json --output /logs/agent",
            timeout_sec=120,
        )
