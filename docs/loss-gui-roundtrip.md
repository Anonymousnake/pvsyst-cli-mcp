# 原生损失参数的保存与再编辑

PVsyst8.1.6的GUI保存可能改写显示名、数值精度、模式位和缓存。GUI载入或计算结果相同，不表示文件逐字节一致，也不自动扩大适配器的编辑范围。

## 字段与精度

- LIDLoss和PMismMPP可由GUI从三位小数重写为两位小数；应区分数值含义与保存格式。
- ModuleQualityLoss的适配器语义成对设置质量U/I字段；原生另存还可能更新P缓存。P字段保持只读，不因此增加独立配置。
- UFactor属于主阵列，不是逆变器温度。热参数写入器仅接受原有Flags=$00100400、Flags2=$082A范围。原生另存可能产生$01900400或$03900400并改写MVNbTransfos、质量缓存及ACWireNoSect；这些状态不自动获得再次编辑支持。

不清除新GUI标志来绕过源检查，也不把GUI副作用复制到适配器。需要回到原工作副本时，使用事务备份及受保护恢复。

## 入口分工

已有标量入口为main_array_updates.UFactor及subarray_updates.LIDLoss、ModuleQualityLoss、PMismMPP；详细模式见[损失参数](native-detailed-losses.md)。AC、DC及辅助功率分别使用对应原生入口，不凭控件或字符串存在推断可配置／可消费。

“组串间电压失配”等可见控件不等于已提供MCP配置。GUI导出、保存状态和原生实际计算是不同验证层，不以旧教程或控件存在替代当前入口支持范围。
