
Feature: Nadi session runtime
  Nadi must preserve the core Aquifer invariants while running locally.

  Scenario: Echo command is durably recorded
    Given a local Nadi stack
    When I create a session for tenant "gherkin"
    And I send an echo command with text "hello gherkin"
    Then the event log contains "assistant.message" with text "hello gherkin"
    And the broker tool path count is 0

  Scenario: Tool command bypasses the broker
    Given a local Nadi stack
    When I create a session for tenant "gherkin"
    And I send an uppercase tool command with text "nadi"
    Then the event log contains "tool.result" with text "NADI"
    And the sandbox tool call count is 1
    And the broker tool path count is 0

  Scenario: Cells reconstruct from event log
    Given a local Nadi stack
    When I create a session for tenant "gherkin"
    And I send an echo command with text "replay me"
    And I reconstruct the session cell
    Then the reconstructed cell has message "replay me"
