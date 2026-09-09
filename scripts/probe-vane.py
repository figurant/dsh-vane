"""Run a real Vane probe; no DuckDB replacement or mocked success."""
import json
import tempfile
import threading
import time
import vane

vane.configure(runner="local")
with tempfile.TemporaryDirectory() as directory:
    c = vane.connect(directory + "/probe.vane")
    c.execute("CREATE TABLE probe AS SELECT 41::BIGINT n")
    vane.attach_function(vane.func(lambda n: n + 1, return_dtype="BIGINT"), connection=c, alias="increment", parameters=["BIGINT"])
    assert c.execute("SELECT increment(n) FROM probe").fetchone()[0] == 42
    interrupted = []
    def query():
        try:
            c.execute("SELECT sum(i*j) FROM range(1000000) a(i), range(1000000) b(j)")
        except Exception as e:
            interrupted.append(type(e).__name__)
    thread = threading.Thread(target=query)
    thread.start()
    time.sleep(0.1)
    c.interrupt()
    thread.join(5)
    assert not thread.is_alive() and interrupted
    assert c.execute("SELECT n FROM probe").fetchone()[0] == 41
    print(json.dumps({"vane_version": vane.__version__, "persistent_table": True, "udf": 42, "interrupt": interrupted, "connection_reusable": True}))
    c.close()
