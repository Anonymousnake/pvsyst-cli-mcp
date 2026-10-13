"""Public native storage configuration contract; no native runs or digest assertions."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from smoke_load_profiles import data

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
                for update in ({"soc_min":True},{"soc_max":"0.9"},{"strategy":"power_shift"},{"allow_grid_charge":True}):
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
                (root/"UserData").mkdir()
                (root/"UserData/raw.csv").write_text(data(),encoding="utf-8",newline="")
                prepared_response=await session.call_tool("pvsyst_prepare_load_profile",{"csv_name":"raw.csv","year":2024,"output_name":"system-load.csv","utc_offset_hours":8,"time_basis":"local_standard_interval_start","dry_run":False})
                prepared=json.loads(prepared_response.content[0].text)
                check(prepared["status"]=="prepared","system load prepared through public API")
                start=TEMPLATE.index("  PVObject_GridStorage=pvGridStorage")
                stop=TEMPLATE.index("  End of PVObject pvGridStorage",start)+len("  End of PVObject pvGridStorage\n")
                pure=TEMPLATE[:start]+TEMPLATE[stop:]
                for field in ("    BatteryFile=Fixture.BTR\n","    NBattSerie=1\n","    NBattParall=2\n"):
                    pure=pure.replace(field,"")
                pure=pure.replace("  Version=8.1.6\n","  Version=8.1.6\n  Flags=$00100001\n").replace("Flags=$10010040","Flags=$00")
                (root/"Projects/Storage.VC5").write_text(pure,encoding="utf-8",newline="")
                (root/"ComposPV/Batteries").mkdir(parents=True)
                btr="PVObject_=pvBattery\n  Version=8.1.6\n  Flags=$00\n  BattTechnol=Pb_Sealed_AGM\n  CapNomC10=160\n  AlphaSOC=0.2\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Fixture\n    Model=12V160Ah\n  End of PVObject pvCommercial\nEnd of PVObject pvBattery\n"
                (root/"ComposPV/Batteries/Fixture.BTR").write_text(btr,encoding="utf-8",newline="")
                system_request={"project":"Storage.PRJ","source_variant":"VC5","new_variant":"VC6",
                    "load_manifest_name":"system-load_load.json",
                    "battery":{"file_name":"Fixture.BTR","series_count":4,"parallel_count":4,"monthly_temperature_c":[20]*12,"cycle_sow":1,"static_sow":1},
                    "storage":{"strategy":"self_consumption","soc_min":0.2,"soc_max":0.9,"initial_soc":0.5,"reserve_soc":0.2,"max_charge_ac_kw":3,"max_discharge_ac_kw":5,"charger_max_efficiency_pct":97,"charger_euro_efficiency_pct":95,"battery_inverter_max_efficiency_pct":97,"battery_inverter_euro_efficiency_pct":95}}
                async def system_call(**changes):
                    response=await session.call_tool("pvsyst_create_grid_storage_system",{**system_request,**changes})
                    return response,None if response.is_error else json.loads(response.content[0].text)
                response,plan=await system_call()
                check(not response.is_error and plan["status"]=="ready","PV-to-storage system preview: "+repr(response if response.is_error else plan))
                check(not (root/"Projects/Storage.VC6").exists(),"system preview zero writes")
                check(plan["backend"]=="pvsyst_native_ac_coupled" and not plan["native_consumption_verified"],"native AC backend and no implied runtime")
                _,built=await system_call(dry_run=False)
                check(built["status"]=="created","create native AC storage system")
                output=(root/"Projects/Storage.VC6").read_text(encoding="utf-8")
                check("Flags=$00180001" in output,"self-consumption root enabled while other bits retained")
                check("PVObject_UserNeeds=pvLoad" in output and "Day_2902=" in output,"explicit complete load installed")
                check("NBattSerie=4" in output and "NBattParall=4" in output,"declared battery hardware installed")
                check((root/"Projects/Storage.VC5").read_text(encoding="utf-8")==pure,"PV-only source retained")
                _,repeat=await system_call(dry_run=False)
                check(repeat["status"]=="blocked","system target not overwritten")
                _,existing=await system_call(source_variant="VC0",new_variant="VC7",dry_run=False)
                check(existing["status"]=="blocked" and not (root/"Projects/Storage.VC7").exists(),"existing storage not silently replaced")
                for bad in ({"series_count":0},{"monthly_temperature_c":[20]*11},{"cycle_sow":0},{"file_name":"../Fixture.BTR"}):
                    _,result=await system_call(new_variant="VC7",battery={**system_request["battery"],**bad},dry_run=False)
                    check(result["status"]=="blocked" and not (root/"Projects/Storage.VC7").exists(),"bad system hardware declaration "+repr(bad))
                response,_=await system_call(new_variant="VC7",storage={"strategy":"self_consumption"})
                check(response.is_error,"missing system SOC/power/efficiency declarations not inferred")
                response,_=await system_call(new_variant="VC7",battery={**system_request["battery"],"series_count":True})
                check(response.is_error,"strict system pack selector")
                a,b=await asyncio.gather(system_call(new_variant="VC8",dry_run=False),system_call(new_variant="VC8",dry_run=False))
                check(sorted([a[1]["status"],b[1]["status"]])==["blocked","created"],"system same-name concurrency has one winner")
                _,invalid_soc=await system_call(new_variant="VC9",storage={**system_request["storage"],"soc_min":0.8},dry_run=False)
                check(invalid_soc["status"]=="blocked" and not (root/"Projects/Storage.VC9").exists(),"invalid combined system SOC rejected before writes")
                response,_=await system_call(new_variant="VC9",storage={**system_request["storage"],"port_count":2})
                check(response.is_error,"DC ports not silently treated as native AC fields")
                response,missing_availability=await system_call(new_variant="VC9",storage={**system_request["storage"],"strategy":"weak_grid"})
                check(not response.is_error and missing_availability["status"]=="blocked","unconfigured weak-grid system not declared ready")
                (root/"Projects/Storage.VC10").write_text(pure.replace("Flags=$00100001","Flags=invalid"),encoding="utf-8",newline="")
                _,invalid_flags=await system_call(source_variant="VC10",new_variant="VC9",dry_run=False)
                check(invalid_flags["status"]=="blocked" and not (root/"Projects/Storage.VC9").exists(),"ambiguous enabling flags rejected before writes")
                from datetime import datetime,timedelta
                origin=datetime(2024,1,1)
                availability="Date;Grid Available\n;ratio\n"+"".join((origin+timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")+";"+("0" if h<24 else "1")+"\n" for h in range(8784))
                (root/"UserData/grid.csv").write_text(availability,encoding="ascii",newline="")
                declaration={"csv_name":"grid.csv","year":2024,"utc_offset_hours":8,"time_basis":"local_standard_interval_start"}
                weak={"source_variant":"VC6","new_variant":"VC11","updates":{"strategy":"weak_grid","initial_soc":0.4,"reserve_soc":0.5},"availability":declaration}
                response,plan=await call(**weak)
                check(not response.is_error and plan["status"]=="ready","WeakGrid public preview: "+repr(plan))
                check(plan["grid_availability"]["rows"]==8784 and plan["grid_availability"]["unavailable_hours"]==24,"full native availability coverage")
                check(not (root/"Projects/Storage.VC11").exists(),"WeakGrid preview zero writes")
                _,made=await call(**weak,dry_run=False)
                check(made["status"]=="created","native WeakGrid variant created")
                weak_text=(root/"Projects/Storage.VC11").read_text(encoding="utf-8")
                check("Strategy=WeakGrid" in weak_text and "PVObject_GridUnavail=pvUnavail" in weak_text,"actual native strategy and object serialized")
                check("HourlyVal=GrdUnav" in weak_text and "VarType=Factor" in weak_text and "Day_2902=" in weak_text,"native ratio snapshot with leap day")
                check((root/"UserData/grid.csv").read_text(encoding="ascii")==availability,"availability input retained")
                _,again=await call(**weak,dry_run=False)
                check(again["status"]=="blocked","WeakGrid no overwrite")
                _,missing=await call(source_variant="VC6",new_variant="VC12",updates={"strategy":"weak_grid"},dry_run=False)
                check(missing["status"]=="blocked" and not (root/"Projects/Storage.VC12").exists(),"WeakGrid missing availability blocked")
                _,wrong_strategy=await call(source_variant="VC6",new_variant="VC12",updates={"initial_soc":0.4},availability=declaration,dry_run=False)
                check(wrong_strategy["status"]=="blocked","availability not silently attached to SelfConsumption")
                for name,value in (("all.csv",availability.replace(";0\n",";1\n")),("fraction.csv",availability.replace(";0\n",";0.5\n",1)),("gap.csv",availability.replace("2024-02-29 00:00;1\n",""))):
                    (root/"UserData"/name).write_text(value,encoding="ascii",newline="")
                    _,bad=await call(**{**weak,"new_variant":"VC12","availability":{**declaration,"csv_name":name}},dry_run=False)
                    check(bad["status"]=="blocked" and not (root/"Projects/Storage.VC12").exists(),"bad native availability "+name)
                response,_=await call(**{**weak,"new_variant":"VC12","availability":{**declaration,"year":True}})
                check(response.is_error,"strict availability year")
                _,year_mismatch=await call(**{**weak,"new_variant":"VC12","availability":{**declaration,"year":2023}},dry_run=False)
                check(year_mismatch["status"]=="blocked","availability year mismatch not inferred")
                _,rebound=await call(**{**weak,"source_variant":"VC11","new_variant":"VC12"},dry_run=False)
                check(rebound["status"]=="created" and (root/"Projects/Storage.VC12").read_text(encoding="utf-8").count("PVObject_GridUnavail=pvUnavail")==1,"existing availability snapshot replaces only owning object")
                weak_system={"new_variant":"VC13","storage":{**system_request["storage"],"strategy":"weak_grid","reserve_soc":0.5},"availability":declaration}
                response,weak_plan=await system_call(**weak_system)
                check(not response.is_error and weak_plan["status"]=="ready","PV-only to WeakGrid preview")
                check(not (root/"Projects/Storage.VC13").exists() and weak_plan["grid_availability"]["rows"]==8784,"direct WeakGrid preview zero writes and coverage")
                _,weak_created=await system_call(**weak_system,dry_run=False)
                check(weak_created["status"]=="created","direct native WeakGrid construction")
                weak_built=(root/"Projects/Storage.VC13").read_text(encoding="utf-8")
                check("Strategy=WeakGrid" in weak_built and "HourlyVal=GrdUnav" in weak_built and "SOCDischSelf=0.5" in weak_built,"native strategy availability and reserve serialized")
                check("Day_2902=" in weak_built and "StoredUnit=MJ" in weak_built and "NBattParall=4" in weak_built,"native leap-year loads and declared battery pack retained")
                check((root/"Projects/Storage.VC5").read_text(encoding="utf-8")==pure,"direct WeakGrid original PV design preserved")
                _,wrong_offset=await system_call(**{**weak_system,"new_variant":"VC14","availability":{**declaration,"utc_offset_hours":9}},dry_run=False)
                check(wrong_offset["status"]=="blocked" and not (root/"Projects/Storage.VC14").exists(),"load and availability offset must agree")
                _,extra_availability=await system_call(new_variant="VC14",availability=declaration,dry_run=False)
                check(extra_availability["status"]=="blocked","SelfConsumption not silently changed by availability")
                response,_=await system_call(**{**weak_system,"new_variant":"VC14","availability":{**declaration,"year":True}})
                check(response.is_error,"direct WeakGrid strict availability declaration")
                (root/"Projects/Storage.VC15").write_text(pure.replace("End of PVObject pvVCalcul","  PVObject_GridUnavail=pvUnavail\n    Flags=$00\n  End of PVObject pvUnavail\nEnd of PVObject pvVCalcul"),encoding="utf-8",newline="")
                _,existing_grid=await system_call(**{**weak_system,"source_variant":"VC15","new_variant":"VC14"},dry_run=False)
                check(existing_grid["status"]=="blocked" and not (root/"Projects/Storage.VC14").exists(),"existing grid profile not duplicated or silently replaced")
                response=await session.call_tool("pvsyst_build_sfi",{"filename":"storage-export.sfi","variables":"grid_storage"})
                check(not response.is_error,"native storage export preset public API")
                export=json.loads(response.content[0].text)
                check({"viCL_Chrg","viCL_InvB","viCL_Over","viEUnused","viE_Miss","viSOC_End"}<=set(export["variables"]),"all native energy-loss and unmet-load terms exported")
                check("viCL_Over" in Path(export["sfi"]).read_text(encoding="utf-8"),"native CL_Over actually in SFI")
                response=await session.call_tool("pvsyst_build_sfi",{"filename":"storage-export.sfi","variables":"grid_storage"})
                check(response.is_error,"native export preset cannot overwrite definition")
    print(f"{checks} public storage configuration checks passed; 0 native runs")

if __name__=="__main__":
    asyncio.run(main())
