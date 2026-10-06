"""AgentCore Platform v1.0"""

# Export the agent class so `module: "src.graph"` in agent.yaml resolves correctly.
from src.graph.graph import NPIComplianceChecklistAgent

__all__ = ["NPIComplianceChecklistAgent"]
