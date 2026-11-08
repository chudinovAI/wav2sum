from wav2sum import diarizer


def test_short_backchannels_are_not_split_into_a_second_person(monkeypatch):
    # On the 20.10 1-on-1 call the default Fb 0.8 made 18 s of "okay"/"mm" a second person; Fb 2.0 keeps one.
    class Pipeline:
        def instantiate(self, params):
            self.params = params
            return self

        def to(self, device):
            pass

    monkeypatch.setenv("HF_TOKEN", "token")
    monkeypatch.setattr(diarizer.Pipeline, "from_pretrained", lambda *args, **kwargs: Pipeline())
    assert diarizer.Diarizer("cpu").pipeline.params == {"clustering": {"Fb": 2.0}}


def test_prepared_pipeline_needs_no_token(monkeypatch, tmp_path):
    class Pipeline:
        def instantiate(self, params):
            pass

        def to(self, device):
            pass

    seen = []
    monkeypatch.setattr(diarizer, "snapshot_path", lambda *args: tmp_path)
    monkeypatch.setattr(diarizer, "get_token", lambda: (_ for _ in ()).throw(AssertionError("token read")))
    monkeypatch.setattr(
        diarizer.Pipeline, "from_pretrained", lambda path, **kwargs: seen.append((path, kwargs)) or Pipeline()
    )
    diarizer.Diarizer("cpu")
    assert seen == [(tmp_path, {"token": None})]
    assert diarizer.os.environ["PYANNOTE_METRICS_ENABLED"] == "false"
