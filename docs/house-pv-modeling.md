# 房屋与光伏：PVCollada2

`pvsyst_create_house_pv_scene(project, variant, output_name, house, layout, dry_run=True, module_geometry=None)`接收完整命名房屋网格和PV布局，薄序列化为标准pvc2。它不执行提交代码、不写原生VC、不操作GUI或实现遮挡／辐照。创建、导入、保存和消费为独立状态；create的imported及native_consumption_verified为false。

完整工程／年度流程用[住宅单任务](native-house-task.md)；双源作者与原生任务另按[双朝向范围](grouped-house-scenes.md)。原固定无阴影入口不因场景能力放开。

## 完整网格house

使用kind=mesh和meshes数组，每构件明确给name、vertices、triangles。name为1..120字符、无首尾空白、忽略大小写唯一；vertices为局部米制[x,y,z]，triangles为零基[i,j,k]、在界且互异，复杂多边形由调用方先三角化。

一次提供墙、屋面、挑檐、门窗外形、烟囱、门廊和同栋附属体量等必要构件，尺寸有依据或明确假设，不将推断当测绘。名称不赋予玻璃／材料／建筑物理语义，闭合、自交、法向及遮挡仍须原生审查。无家具、精细五金、材质、周边树木／建筑或通用CAD内核。

上限128构件、50000顶点、100000三角，属于接口负载界。坐标六位小数，按实际序列化值检查退化、微小非零归零及派生溢出。响应只返回摘要。issues及diagnostics含code、path、message、repair_hint，适用时含part／triangle；例如house.meshes[3].triangles[7]。最多100项并披露diagnostics_truncated，不自动修面。

## 显式PV布局

网格单区layout恰含七字段：module_rows、module_columns、orientation、row_spacing_m、column_spacing_m、origin_m、roof_mesh。行列严格正整数；方向portrait/landscape；间距非负米；origin_m为低侧局部起点；roof_mesh是精确的独立共面向上屋面网格，不是实体盒。

倾角／方位继承源普通固定朝向。屋面法向与PV一致，四角竖直投影落在屋面三角并集且不低于屋面。该检查不证明连通、无洞或整块不跨凹口，也不替代实体相交／结构校验。

多区使用tables，每项加name及同样七字段。名字仅在tables内忽略大小写唯一，支持1..64区；行列乘积之和等于源组件总数，不在每区复制整个阵列。每区独立标准rack/table/instance，共用源PAN及角度。共面PV矩形内部重叠返回pv_tables_overlap，接边不算。双源输入的orientation_id及保守投影检查另见[分组作者](grouped-house-scenes.md)。

## 组件与旧参数

module_geometry明确包含model_name、nom_power_wp、width_m、length_m、geometry_depth_m。可读PAN优先工作区后配置的loose库，解析根或商业米制尺寸；重复／冲突停止，与声明型号／功率／尺寸核对并写前重验。

数据库引用没有独立PAN时，显式geometry返回caller-provided-for-native-database-reference，不认证数据库内容。可读PAN也需要明确厚度，因此完整geometry实际创建仍必需，默认None只用于返回缺项，不猜厂家厚度。

旧参数兼容length_m、width_m、eave_height_m、roof.kind=flat/gable；gable的pitch_deg满足0<pitch_deg<90且与源倾角一致，屋脊平行局部X，前坡放PV。layout明确行列、方向、行列间距、clearance_m及edge_margin_m；不自动摆满。退距在平顶按水平、双坡按坡面解释，roof_rise_m相对檐口而非绝对标高。

## 读回与修订

`pvsyst_read_house_pv_scene(scene_name)`只读UserData中的有界UTF-8pvc2，排除DTD／实体声明。支持方言从真实XML取网格／rack／几何元数据，重建并进行命名空间归一后的完整比较。未知节点、属性、变换、材料、动画或坏元数据停止，不静默另写“相似”场景。

ready表示可重建，非工程认证。create_arguments供原create预览，新output_name始终由调用方提供；source.provenance=file-declared及source_verified=false。旧的可识别不完整方言返回needs_context及草案，缺project／variant／roof_mesh不猜。重新创建会重新读取实际工程和PAN，不把文件声明当认证。

修订为读回、修改指定输入、指定新名、预览、按诊断修正、创建新文件，再原生导入／保存。不存在补丁DSL、私有VC写入或自动覆盖。预览不创建目录；执行仅新UserData文件，大小写同名、越界路径、未知键、错误数值／XML字符停止，源工程与PAN保持。

## 原生自动导入

`pvsyst_import_house_pv_scene(scene_name, variant_name, dry_run=True, confirm=False)`使用已打开、匹配DataPath／源工程、已保存且无弹窗的GUI，不启动／退出软件、不写INI。仅支持中文8.1.6rev49278、DPI96及录制物理布局：3D编辑器1475×899、朝向管理1126×826，不承诺任意缩放。

预览不激活、输入或读剪贴板；实际需dry_run=false且confirm=true，期间保持单一桌面驱动。预检重用reader及源／PAN，按六位精度核对引用、站点和角度。GUI关联仅1..2区，作者64区不是导入覆盖保证。未知模态、窗口、动作或警告停止并保留现场。

原生链依次导入、朝向关联、有效性、遮挡表、另存新VC。PVC2当前有beta提示，导入自动物体名不保证源构件名保真。Shift+Ctrl+O关联到原系统朝向时选择实际PV参数动作，不启用平均；仅删除已确认空组，不删除PV场区／子阵。显示模型不等于关联完成，保存后须核对并重载。

有效性保留native_validity正文；native_scene_invalid不保存。Ctrl+C仅在本次复制序号变化后读取新文本，短暂锁有界等待，序号／前台／外来owner异常停止；不读取或备份先前剪贴板。DPI上下文局部线程使用并恢复，不改全进程。

imported／native_geometry_verified只在新VC保存及所支持对象／来源／朝向／组件和硬件后验通过后为true，不等于通用源坐标认证。native_consumption_verified仍false；实际消费另用run_simulation(recompute_shading=true)和新CSV/PDF。保存后后验失败仍披露已存路径，不重放或覆盖源。受限双屋面另须[严格空间核对](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。

## 标准与解释

COLLADA1.5、米制、Z_UP；PV namespace为https://pvcollada.org/2026/XMLSchema，profile=PVCollada-2.0，只有pv:module尺寸转换为毫米。普通构件不发明pv:house/window标签。共同父Z旋转使用北半球PVsyst方位到地理北的标准转换，PV平移仍为房屋局部；原生可合并共面三角，不保证分面形式保持。

原生几何有效不认证建筑，全年差值不单独归因某个构件，缺GlobShd等列不补零。支持源为单普通固定Grid、完整计数及内嵌站点，无活动场景／布局、负荷／储能、P50、地平线；双源仅按单独范围，不泛化任意类型。

参考[原生格式](https://www.pvsyst.com/help/project-design/shadings/near-shadings-import/pv-collada-file-format.html)及[朝向管理](https://www.pvsyst.com/help/project-design/shadings/near-shadings-orientations/orientation-management-window.html)。
