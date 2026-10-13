"""Public native storage configuration contract; no native runs or digest assertions."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER=Path(__file__).resolve().parents[1]/"pvsyst_mcp_server.py"
TEMPLATE="""PVObject_=pvVCalcul
  Comment=Storage fixture
  Version=8.1.6
  PVObject_=pvSubArray
    SubArrayId=1
    NModSerie=10
  End of PVObject pvSubArray
  PVObject_System=pvSystem
    Flags=$10010040
    SystemType=Grid
    BatteryFile=Fixture.BTR
    NBattSerie=1
    NBattParall=2
  End of PVObject pvSystem
  PVObject_GridStorage=pvGridStorage
    Flags=$00
    Strategy=SelfConsumption
    ChargingStrategy=PVAvailable
    DischStrategy=Immediate
    BattSoCInit=0.6
    SOCChDecl=0.95
    SOCDischDecl=0.2
    SOCDischSelf=0.5
    PMaxCharge=380
    PMaxDisch=170
    BattRectMaxEffic=97
    BattRectEuroEffic=95
    BattInvMaxEffic=97
    BattInvEuroEffic=95
  End of PVObject pvGridStorage
End of PVObject pvVCalcul
"""

async def main():
    checks=0
    def check(value,message):
        nonlocal checks
        if not value:
            raise AssertionError(message)
        checks+=1
    with tempfile.TemporaryDirectory(prefix="pvsyst-storage-contract-") as directory:
        root=Path(directory)
        (root/"Projects").mkdir()
        (root/"Projects/Storage.PRJ").write_text("PVObject_=pvProject\n  Comment=Storage fixture\nEnd of PVObject pvProject\n",encoding="utf-8")
        (root/"Projects/Storage.VC0").write_text(TEMPLATE,encoding="utf-8",newline="")
        env={**os.environ,"PVSYST_CLI":sys.executable,"PVSYST_WORKSPACE":str(root)}
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(SERVER)],env=env)) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                base={"project":"Storage.PRJ","source_variant":"VC0","new_variant":"VC1",
                      "updates":{"initial_soc":0.4,"soc_max":0.8,"max_charge_ac_kw":1,"max_discharge_ac_kw":1}}
                async def call(**changes):
                    response=await session.call_tool("pvsyst_create_grid_storage_variant",{**base,**changes})
                    return response,None if response.is_error else json.loads(response.content[0].text)
                response,preview=await call()
                check(not response.is_error and preview["status"]=="ready","preview ready")
                check(not (root/"Projects/Storage.VC1").exists(),"preview zero writes")
                check(preview["after"]["initial_soc"]==0.4 and preview["after"]["soc_max"]==0.8,"declared SOC mapping")
                check(preview["after"]["max_charge_ac_kw"]==1 and preview["after"]["max_discharge_ac_kw"]==1,"explicit AC scopes")
                check(not preview["native_consumption_verified"],"saved values not certified")
                _,created=await call(dry_run=False)
                check(created["status"]=="created","new variant created")
                check((root/"Projects/Storage.VC0").read_text(encoding="utf-8")==TEMPLATE,"source preserved")
                copied=(root/"Projects/Storage.VC1").read_text(encoding="utf-8")
                check("BatteryFile=Fixture.BTR" in copied and "NBattParall=2" in copied,"pack identity and counts retained")
                check("Flags=$10010040" in copied and "NModSerie=10" in copied,"system/topology unchanged")
                _,exists=await call(dry_run=False)
                check(exists["status"]=="blocked","no overwrite")
                invalid=[{"soc_min":0.99},{"soc_max":0.1},{"initial_soc":0.1},{"reserve_soc":0.1},
                         {"max_charge_ac_kw":0},{"max_discharge_ac_kw":-1},{"charger_max_efficiency_pct":94},
                         {"battery_inverter_euro_efficiency_pct":99}]
                for update in invalid:
                    _,result=await call(new_variant="VC2",updates=update,dry_run=False)
                    check(result["status"]=="blocked" and not (root/"Projects/Storage.VC2").exists(),"invalid semantic update: "+repr(update))
                for update in ({"soc_min":True},{"soc_max":"0.9"},{"strategy":"weak_grid"},{"allow_grid_charge":True}):
                    response,_=await call(new_variant="VC2",updates=update,dry_run=False)
                    check(response.is_error,"strict type/unsupported capability: "+repr(update))
                response,_=await call(unknown=1)
                check(response.is_error,"unknown top-level argument")
                _,escape=await call(new_variant="../VC2",dry_run=False)
                check(escape["status"]=="blocked","variant stays in project namespace")
                _,empty=await call(new_variant="VC2",updates={})
                check(empty["status"]=="blocked","empty configuration not guessed")
                dormant=TEMPLATE.replace("Flags=$10010040","Flags=$00010000")
                (root/"Projects/Storage.VC3").write_text(dormant,encoding="utf-8",newline="")
                _,inactive=await call(source_variant="VC3",new_variant="VC4",dry_run=False)
                check(inactive["status"]=="blocked" and not (root/"Projects/Storage.VC4").exists(),"dormant storage fields are not an enabled template")
    print(f"{checks} public storage configuration checks passed; 0 native runs")

if __name__=="__main__":
    asyncio.run(main())
