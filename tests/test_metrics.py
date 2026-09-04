import unittest
from ai_game_player.action_executor import ExecutionResult
from ai_game_player.metrics import MetricsCalculator
class MetricsTest(unittest.TestCase):
    def test_calculates_execution_summary(self):
        result=MetricsCalculator().calculate([ExecutionResult("a",False,"dry_run",""),ExecutionResult("a",True,"live",""),ExecutionResult("b",False,"failed","")])
        self.assertEqual((result.total,result.dry_run,result.executed,result.failed),(3,1,1,1)); self.assertEqual(result.action_counts,{"a":2,"b":1})