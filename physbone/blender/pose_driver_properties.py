"""Clothing author rules persisted in .blend and exposed to the SDK pose bridge.

Manual controls remain preview-only; the separate pose resource does not alter HD2PHY1.
"""
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup

AXES = tuple((axis, axis, '相对参考姿态的局部轴，不是世界轴') for axis in 'XYZ')


class HD2PB_PoseSample(PropertyGroup):
    name: StringProperty(name='采样点名称', default='采样点')
    input_angle: FloatProperty(name='输入角度（度）', min=-180, max=180, precision=2)
    output_xyz: FloatVectorProperty(name='目标局部 XYZ（度）', size=3, min=-180, max=180, precision=2)


class HD2PB_PoseAxis(PropertyGroup):
    name: StringProperty(name='输入轴')
    samples: CollectionProperty(type=HD2PB_PoseSample)
    sample_index: IntProperty(default=0, min=0)
    invert_input: BoolProperty(name='反转本轴输入')
    secondary_axis: EnumProperty(name='第二源骨对应轴', items=AXES, default='X')
    invert_secondary: BoolProperty(name='反转第二输入')


class HD2PB_PoseDriver(PropertyGroup):
    marker_id: StringProperty(options={'HIDDEN'})
    color_backup: StringProperty(options={'HIDDEN'})
    axis_curves: CollectionProperty(type=HD2PB_PoseAxis)
    name: StringProperty(name='驱动名称', default='衣物姿态驱动')
    enabled: BoolProperty(name='启用', default=True)
    source_bone: StringProperty(name='源骨')
    secondary_bone: StringProperty(name='第二源骨')
    reference_bone: StringProperty(name='参考骨', description='留空时分别使用源骨的父骨；可指定 pelvis')
    target_bone: StringProperty(name='衣物目标骨', description='创建时绑定选中骨；更换目标请删除条目后重新创建，不自动加骨或改权重')
    input_axis: EnumProperty(name='编辑输入轴', description='只切换采样列表；预览与运行时均计算全部三个输入轴', items=AXES, default='X')
    combination: EnumProperty(name='双输入组合', items=(
        ('SINGLE', '单源骨', '只使用源骨'),
        ('MAX', '取较大响应', '每输入轴中，两源分别映射，再逐输出 XYZ 分量取数值较大者'),
        ('MIN', '取较小响应', '每输入轴中，两源分别映射，再逐输出 XYZ 分量取数值较小者'),
        ('ADD', '响应相加', '每输入轴中，两源输出 XYZ 分量相加并夹至正负180度，再合计三个输入轴贡献')), default='SINGLE')
    influence: FloatProperty(name='目标影响', default=1, min=0, max=1,
        description='从原动画局部旋转混合到 Rest 加曲线偏移；不在上一帧结果上累加')
    calibrated: BoolProperty(default=False, options={'HIDDEN'})
    calibration_binding: StringProperty(options={'HIDDEN'})
    neutral_primary: FloatVectorProperty(size=4, default=(1, 0, 0, 0), options={'HIDDEN'})
    neutral_secondary: FloatVectorProperty(size=4, default=(1, 0, 0, 0), options={'HIDDEN'})
    use_manual_input: BoolProperty(name='手动测试输入（不改源骨）', description='仅影响 Blender 预览；开关和测试角度不导出，游戏使用真实动画输入', default=False)
    test_xyz: FloatVectorProperty(name='主输入 XYZ（度）', size=3, min=-180, max=180)
    test_secondary_xyz: FloatVectorProperty(name='第二输入 XYZ（度）', size=3, min=-180, max=180)


CLASSES = (HD2PB_PoseSample, HD2PB_PoseAxis, HD2PB_PoseDriver)
