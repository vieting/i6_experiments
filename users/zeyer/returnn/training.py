"""
RETURNN training utils
"""

import os
import subprocess
import copy
from sisyphus import gs, tk, Job, Task
from i6_core.returnn.training import Checkpoint
from i6_core.returnn.config import ReturnnConfig
import i6_core.util as util


class ReturnnInitModelJob(Job):
    """
    Initialize the model. task="initialize_model" in RETURNN.

    Only returnn_config, returnn_python_exe and returnn_root influence the hash.

    The outputs provided are:

     - out_returnn_config_file: the finalized Returnn config which is used for the rnn.py call
     - out_checkpoint:
    """

    def __init__(
        self,
        returnn_config,
        *,
        log_verbosity=3,
        time_rqmt=1,
        mem_rqmt=4,
        cpu_rqmt=1,
        returnn_python_exe=None,
        returnn_root=None,
    ):
        """

        :param ReturnnConfig returnn_config:
        :param int log_verbosity: RETURNN log verbosity from 1 (least verbose) to 5 (most verbose)
        :param int|float time_rqmt:
        :param int|float mem_rqmt:
        :param int cpu_rqmt:
        :param Path|str returnn_python_exe: file path to the executable for running returnn (python binary or .sh)
        :param Path|str returnn_root: file path to the RETURNN repository root folder
        """
        assert isinstance(returnn_config, ReturnnConfig)
        kwargs = locals()
        del kwargs["self"]

        self.returnn_python_exe = (
            returnn_python_exe
            if returnn_python_exe is not None
            else getattr(gs, "RETURNN_PYTHON_EXE")
        )
        self.returnn_root = (
            returnn_root if returnn_root is not None else getattr(gs, "RETURNN_ROOT")
        )
        self.returnn_config = self.create_returnn_config(**kwargs)

        self.out_returnn_config_file = self.output_path("returnn.config")
        self.out_model_dir = self.output_path("models", directory=True)
        self.out_checkpoint = Checkpoint(index_path=self.output_path("models/init.index"))

        self.returnn_config.post_config["model"] = os.path.join(
            self.out_model_dir.get_path(), "init"
        )

        self.rqmt = {
            "cpu": cpu_rqmt,
            "mem": mem_rqmt,
            "time": time_rqmt,
        }

    def _get_run_cmd(self):
        run_cmd = [
            tk.uncached_path(self.returnn_python_exe),
            os.path.join(tk.uncached_path(self.returnn_root), "rnn.py"),
            self.out_returnn_config_file.get_path(),
        ]
        return run_cmd

    def tasks(self):
        """sis job tasks"""
        yield Task("create_files", mini_task=True)
        yield Task("run", resume="run", rqmt=self.rqmt)

    def create_files(self):
        """create config"""
        self.returnn_config.write(self.out_returnn_config_file.get_path())
        util.create_executable("rnn.sh", self._get_run_cmd())

    def run(self):
        """run task"""
        subprocess.check_call(self._get_run_cmd())

    @classmethod
    def create_returnn_config(
        cls,
        returnn_config: ReturnnConfig,
        log_verbosity,
        **_kwargs,
    ) -> ReturnnConfig:
        """create derived and adapted config"""
        res = copy.deepcopy(returnn_config)

        config = {
            "task": "initialize_model",
            "target": "classes",
        }

        post_config = {
            "log": ["./returnn.log"],
            "log_verbosity": log_verbosity,
        }

        config.update(copy.deepcopy(returnn_config.config))
        if returnn_config.post_config is not None:
            post_config.update(copy.deepcopy(returnn_config.post_config))

        res.config = config
        res.post_config = post_config
        res.check_consistency()

        return res

    @classmethod
    def hash(cls, kwargs):
        """hash"""
        d = {
            "returnn_config": cls.create_returnn_config(**kwargs),
            "returnn_python_exe": kwargs["returnn_python_exe"],
            "returnn_root": kwargs["returnn_root"],
        }
        return super().hash(d)


class GetRelevantEpochsFromTrainingJob(Job):
    """
    Collects the most relevant kept epochs from the training job
    based on the training cross validation ("dev_...") scores.

    This is intended to then use to perform recognition on
    (maybe in addition to the anyway fixed kept epochs).

    You can parse the output in another Job.update() function
    to dynamically add corresponding recognition jobs.
    """

    def __init__(self, *,
                 model_dir: tk.Path,
                 model_name: str = "epoch",
                 scores_and_learning_rates: tk.Path,
                 n_best: int = 2):
        """
        :param model_dir: ReturnnTrainingJob.out_model_dir
        :param model_name: RETURNN config `model` option. this is hardcoded to "epoch" in ReturnnTrainingJob
        :param scores_and_learning_rates: ReturnnTrainingJob.out_learning_rates
        :param n_best: number of best epochs to return
        """
        self.model_dir = model_dir
        self.model_name = model_name
        self.scores_and_learning_rates = scores_and_learning_rates
        self.n_best = n_best
        self.output = self.output_path("info.txt")  # epochs. Py format: {"epochs": list[int], ...}

    def run(self):
        """run"""
        score_keys = set()

        # simple wrapper, to eval newbob.data
        # noinspection PyPep8Naming
        def EpochData(learningRate, error):
            """
            :param float learningRate:
            :param dict[str,float] error: keys are e.g. "dev_score_output" etc
            :rtype: dict[str,float]
            """
            assert isinstance(error, dict)
            score_keys.update(error.keys())
            d = {"learning_rate": learningRate}
            d.update(error)
            return d

        # nan/inf, for some broken newbob.data
        nan = float("nan")
        inf = float("inf")

        scores_str = open(self.scores_and_learning_rates.get_path()).read()
        scores = eval(scores_str, {"EpochData": EpochData, "nan": nan, "inf": inf})
        assert isinstance(scores, dict)
        all_epochs = sorted(scores.keys())

        suggested_epochs = set()
        for score_key in score_keys:
            if not score_key.startswith("dev_"):
                continue
            dev_scores = sorted([
                (float(scores[ep][score_key]), int(ep))
                for ep in all_epochs if score_key in scores[ep]])
            assert dev_scores
            if dev_scores[0][0] == dev_scores[-1][0]:
                # All values are the same (e.g. 0.0), so no information. Just ignore this score_key.
                continue
            if dev_scores[0] == (0.0, 1):
                # Heuristic. Ignore the key if it looks invalid.
                continue
            for value, ep in sorted(dev_scores)[:self.n_best]:
                suggested_epochs.add(ep)
                print("Suggest: epoch %i because %s %f" % (ep, score_key, value))

        print("Suggested epochs:", suggested_epochs)
        assert suggested_epochs

        for ep in sorted(suggested_epochs):
            if not self._chkpt_exists(ep):
                print("Model does not exist (anymore):", suggested_epochs)
                suggested_epochs.remove(ep)
        assert suggested_epochs  # after filter

        with open(self.output.get_path(), "w") as f:
            f.write("%r\n" % ({"epochs": sorted(suggested_epochs)},))

    def _chkpt_exists(self, epoch: int) -> True:
        """
        :param int epoch:
        :rtype: bool
        """
        assert tk.running_in_worker()
        possible_fns = [
            "%s/%s.%03d.index" % (self.model_dir.get_path(), self.model_name, epoch),
            "%s/%s.pretrain.%03d.index" % (self.model_dir.get_path(), self.model_name, epoch)]
        for fn in possible_fns:
            if os.path.exists(fn):
                return True
        return False

    def tasks(self):
        """tasks"""
        yield Task('run', rqmt={'cpu': 1, 'mem': 1, 'time': 0.1}, mini_task=True)
