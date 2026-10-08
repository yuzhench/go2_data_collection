"""Offload regressions; no robot or network needed."""
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

module_path = Path(__file__).with_name('go2_offload.py')
spec = importlib.util.spec_from_file_location('offload', module_path)
offload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offload)


class OffloadTests(unittest.TestCase):
    def test_wrong_size_existing_small_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'task/date/time/first_person/scan.npy'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'x')
            contents = b'correct bytes'
            response = io.BytesIO(len(contents).to_bytes(8, 'little') + contents)
            response.status = 200
            conn = Mock()
            conn.getresponse.return_value = response
            robot = offload.Robot('localhost:8010')
            worker = offload.Offloader(robot, Path(tmp))
            with patch.object(offload.http.client, 'HTTPConnection', return_value=conn):
                worker._download_pack('task/date/time', [{'path':'first_person/scan.npy', 'size':len(contents)}])
            self.assertEqual(target.read_bytes(), contents)

    def test_export_does_not_read_files_if_server_is_busy(self):
        robot = Mock()
        robot.post_json.side_effect = OSError('Recording in progress')
        worker = offload.Offloader(robot, Path('/unused'))
        worker.selected = ['task/date/time']
        worker._run_job()
        self.assertEqual(worker.phase, 'error')
        robot.get_json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
