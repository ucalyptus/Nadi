
import re
import unittest
from pathlib import Path

from nadi.runtime import local_stack

FEATURE = Path(__file__).resolve().parents[1] / "features" / "nadi_acceptance.feature"


class MiniGherkinRunner(unittest.TestCase):
    """Tiny stdlib Gherkin runner for acceptance criteria in features/*.feature."""

    def test_feature_file_scenarios_pass(self):
        text = FEATURE.read_text()
        scenarios = [s for s in re.split(r"\n\s*Scenario:\s*", text) if "Given " in s]
        self.assertGreaterEqual(len(scenarios), 3)
        for scenario in scenarios:
            name, *body = scenario.splitlines()
            with self.subTest(scenario=name.strip()):
                ctx = {}
                for raw in body:
                    step = raw.strip()
                    if not step or step.startswith("#"):
                        continue
                    self.run_step(ctx, step)

    def run_step(self, ctx, step):
        if step == "Given a local Nadi stack":
            ctx["stack"] = local_stack(":memory:")
            ctx["gateway"] = ctx["stack"]["gateway"]
            return
        m = re.match(r'When I create a session for tenant "([^"]+)"', step)
        if m:
            ctx["session_id"] = ctx["gateway"].create_session(m.group(1))["session_id"]
            return
        m = re.match(r'And I send an echo command with text "([^"]*)"', step)
        if m:
            ctx["gateway"].send_command(ctx["session_id"], "echo", {"text": m.group(1)})
            return
        m = re.match(r'And I send an uppercase tool command with text "([^"]*)"', step)
        if m:
            ctx["gateway"].send_command(ctx["session_id"], "tool", {"name": "uppercase", "args": {"text": m.group(1)}})
            return
        if step == "And I reconstruct the session cell":
            ctx["stack"]["celld"].cells.pop(ctx["session_id"])
            ctx["reconstructed"] = ctx["stack"]["celld"].reconstruct_cell(ctx["session_id"])
            return
        m = re.match(r'Then the event log contains "([^"]+)" with text "([^"]*)"', step)
        if m:
            event_type, text = m.groups()
            events = ctx["gateway"].get_session_events(ctx["session_id"])
            haystack = [e for e in events if e["event_type"] == event_type]
            self.assertTrue(haystack, f"missing event type {event_type}")
            self.assertIn(text, str(haystack))
            return
        m = re.match(r'And the broker tool path count is (\d+)', step)
        if m:
            self.assertEqual(ctx["stack"]["broker"].tool_path_calls, int(m.group(1)))
            return
        m = re.match(r'And the sandbox tool call count is (\d+)', step)
        if m:
            self.assertEqual(ctx["stack"]["sandboxd"].tool_calls, int(m.group(1)))
            return
        m = re.match(r'Then the reconstructed cell has message "([^"]*)"', step)
        if m:
            self.assertIn(m.group(1), ctx["reconstructed"].state["messages"])
            return
        self.fail(f"unimplemented Gherkin step: {step}")


if __name__ == "__main__":
    unittest.main()
