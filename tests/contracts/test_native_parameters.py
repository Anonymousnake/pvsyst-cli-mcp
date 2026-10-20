"""Public metadata contract; synthetic text fixtures require no native executable."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
SCENE = """  PVObject_Ombrage=pvShading
    Flags=$00000100
    ListeObjets, list of=1 TShdObject
      PVObject_1=pvShdGenObject
        Comment=Protocol fixture
      End of PVObject pvShdGenObject
    End of ListeObjets
    Groups, list of=1
      PVObject_=pvShdObjectGroup
        Comment=Group metadata
      End of PVObject pvShdObjectGroup
    End of Groups
  End of PVObject pvShading
"""


def variant(scene):
    return ("PVObject_=pvVCalcul\n  Version=8.1.6\n  Comment=Read contract\n"
            "  PVObject_=pvSubArray\n    SubArrayId=1\n"
            "  End of PVObject pvSubArray\n" + scene + "End of PVObject pvVCalcul\n")


class NativeParametersContract(unittest.IsolatedAsyncioTestCase):
    async def test_scene_list_excludes_group_metadata_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-read-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            (projects / "READ.PRJ").write_text(
                "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
            scenarios = {
                "VC0": "",
                "VC1": SCENE,
                "VC2": SCENE.replace("list of=1 TShdObject", "list of=2 TShdObject"),
                "VC3": SCENE + SCENE,
            }
            for name, scene in scenarios.items():
                (projects / ("READ." + name)).write_text(variant(scene), encoding="utf-8")
            originals = {path: path.read_bytes() for path in projects.iterdir()}
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"Native execution excluded from metadata contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(
                command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name in scenarios:
                        result = await session.call_tool("pvsyst_get_variant_parameters",
                                                         {"project": "READ.PRJ", "variant": name})
                        self.assertFalse(result.is_error, result.content)
                        payload = json.loads("\n".join(item.text for item in result.content
                                                       if hasattr(item, "text")))
                        scene = payload["shading_scene"]
                        if name == "VC1":
                            self.assertEqual(scene["mode"], "active")
                            self.assertEqual(scene["object_count"], 1)
                            self.assertEqual(scene["object_types"], ["pvShdGenObject"])
                        elif name == "VC0":
                            self.assertIsNone(scene["mode"])
                            self.assertIn("reason", scene)
                        else:
                            self.assertIsNone(scene["mode"])
                            self.assertIn("issue", scene)
            self.assertTrue(all(path.read_bytes() == before for path, before in originals.items()))


if __name__ == "__main__":
    unittest.main()
