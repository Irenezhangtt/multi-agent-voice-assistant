"""Harbor adapter for frozen real-call evidence replay, not a new business call."""
from harbor.agents.installed.base import BaseInstalledAgent


class EvidenceReplayAgent(BaseInstalledAgent):
    @staticmethod
    def name():
        return 'clinic-real-trace-replay'

    async def install(self, environment):
        pass

    async def run(self, instruction, environment, context):
        await self.exec_as_agent(environment, 'python -m evals.harbor_judge replay', timeout_sec=30)
