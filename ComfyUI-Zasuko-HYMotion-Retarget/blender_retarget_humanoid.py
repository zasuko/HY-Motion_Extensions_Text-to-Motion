import bpy
import os
import sys
import numpy as np
from math import radians
from mathutils import Matrix, Quaternion, Vector
from io_scene_fbx import import_fbx as blender_fbx_import


# Blender 5.1 FBX importer may fail on embedded lights because an optional
# Cycles property is absent. Lights are irrelevant to retargeting, so replace
# only the failed light with a harmless placeholder and continue importing.
_original_read_light = blender_fbx_import.blen_read_light


def _safe_read_light(fbx_tmpl, fbx_obj, settings):
    try:
        return _original_read_light(fbx_tmpl, fbx_obj, settings)
    except AttributeError as exc:
        if "cast_shadow" not in str(exc):
            raise
        return bpy.data.lights.new("Ignored_FBX_Light", type="POINT")


blender_fbx_import.blen_read_light = _safe_read_light


script_args = sys.argv[sys.argv.index("--") + 1 :]
source_path, target_path, output_path = script_args[:3]
preview_output_path = script_args[3] if len(script_args) > 3 else None

MAPPING = {
    "Pelvis": "mixamorig:Hips",
    "Spine1": "mixamorig:Spine",
    "Spine2": "mixamorig:Spine1",
    "Spine3": "mixamorig:Spine2",
    "Neck": "mixamorig:Neck",
    "Head": "mixamorig:Head",
    "L_Collar": "mixamorig:LeftShoulder",
    "L_Shoulder": "mixamorig:LeftArm",
    "L_Elbow": "mixamorig:LeftForeArm",
    "L_Wrist": "mixamorig:LeftHand",
    "R_Collar": "mixamorig:RightShoulder",
    "R_Shoulder": "mixamorig:RightArm",
    "R_Elbow": "mixamorig:RightForeArm",
    "R_Wrist": "mixamorig:RightHand",
    "L_Hip": "mixamorig:LeftUpLeg",
    "L_Knee": "mixamorig:LeftLeg",
    "L_Ankle": "mixamorig:LeftFoot",
    "L_Foot": "mixamorig:LeftToeBase",
    "R_Hip": "mixamorig:RightUpLeg",
    "R_Knee": "mixamorig:RightLeg",
    "R_Ankle": "mixamorig:RightFoot",
    "R_Foot": "mixamorig:RightToeBase",
}


def import_fbx(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=False)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    arms = [obj for obj in imported if obj.type == "ARMATURE"]
    if len(arms) != 1:
        raise RuntimeError(f"Expected one armature in {path}, found {len(arms)}")
    return arms[0], imported


def world_rest(arm, bone):
    return arm.matrix_world @ bone.matrix_local


def world_pose(arm, pose_bone):
    return arm.matrix_world @ pose_bone.matrix


def compose(location, rotation):
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()


def armature_height(arm):
    points = [world_rest(arm, bone).translation for bone in arm.data.bones]
    return max(p.z for p in points) - min(p.z for p in points)


def evaluated_mesh_min_z(objects):
    """Return the lowest visible deformed-mesh point in world space."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lowest = None
    for obj in objects:
        if obj.type != "MESH" or obj.name not in bpy.data.objects:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        temp_mesh = evaluated.to_mesh()
        try:
            for vertex in temp_mesh.vertices:
                world_z = (evaluated.matrix_world @ vertex.co).z
                lowest = world_z if lowest is None else min(lowest, world_z)
        finally:
            evaluated.to_mesh_clear()
    if lowest is None:
        raise RuntimeError("Target FBX has no mesh vertices for grounding")
    return lowest


def prepare_preview_basecolor(preview_path, objects):
    """Create a richer preview-only base-color texture without touching the source."""
    preview_images = {}
    output_stem = os.path.splitext(preview_path)[0]
    target_materials = {
        slot.material
        for obj in objects
        if obj.type == "MESH" and obj.name in bpy.data.objects
        for slot in obj.material_slots
        if slot.material is not None
    }

    for material in target_materials:
        if not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            base_color = node.inputs.get("Base Color")
            if not base_color:
                continue
            for link in base_color.links:
                texture_node = link.from_node
                source_image = getattr(texture_node, "image", None)
                if texture_node.type != "TEX_IMAGE" or source_image is None:
                    continue

                key = source_image.as_pointer()
                corrected = preview_images.get(key)
                if corrected is None:
                    corrected = source_image.copy()
                    corrected.name = f"{source_image.name}_PreviewColor"
                    pixels = np.empty(len(corrected.pixels), dtype=np.float32)
                    corrected.pixels.foreach_get(pixels)
                    rgba = pixels.reshape((-1, 4))

                    # ComfyUI's FBX preview can display the embedded sRGB base
                    # color brighter and flatter than the GLB/Blender view.
                    # A mild preview-only contrast and saturation compensation
                    # restores the source texture's richer appearance.
                    rgb = np.clip(rgba[:, :3], 0.0, 1.0)
                    luminance = (
                        rgb[:, 0:1] * 0.2126
                        + rgb[:, 1:2] * 0.7152
                        + rgb[:, 2:3] * 0.0722
                    )
                    rgb = luminance + (rgb - luminance) * 1.45
                    rgba[:, :3] = np.power(np.clip(rgb, 0.0, 1.0), 1.15)
                    corrected.pixels.foreach_set(pixels)
                    corrected.update()

                    suffix = "" if not preview_images else f"_{len(preview_images) + 1}"
                    texture_path = f"{output_stem}_basecolor{suffix}.png"
                    corrected.filepath_raw = texture_path
                    corrected.file_format = "PNG"
                    corrected.save()
                    corrected.pack()
                    preview_images[key] = corrected
                    print(f"PREVIEW_COLOR_TEXTURE|{texture_path}")

                texture_node.image = corrected

    return len(preview_images)


bpy.ops.wm.read_factory_settings(use_empty=True)
target_arm, target_objects = import_fbx(target_path)
target_arm.name = "Zasuko_Target_Armature"
for obj in target_objects:
    if obj.animation_data:
        obj.animation_data_clear()
for pose_bone in target_arm.pose.bones:
    pose_bone.matrix_basis.identity()
bpy.context.view_layer.update()

# Mixamo stores the same visible size as Armature scale 0.01 plus Mesh scale
# 100. ComfyUI Load3D uses the un-deformed mesh bounds for camera fitting, so
# it sees an invisible box about 100 times larger than the character. Bake only
# these object transforms before retargeting; the rig, motion and materials stay
# unchanged while the preview bounds become the real character size.
bpy.ops.object.select_all(action="DESELECT")
for obj in target_objects:
    if obj.type in {"ARMATURE", "MESH"}:
        obj.select_set(True)
bpy.context.view_layer.objects.active = target_arm
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
bpy.context.view_layer.update()

source_arm, source_objects = import_fbx(source_path)
source_arm.name = "HYMotion_Source_Armature"

missing = [f"{src}->{dst}" for src, dst in MAPPING.items() if src not in source_arm.pose.bones or dst not in target_arm.pose.bones]
if missing:
    raise RuntimeError("Missing mapped bones: " + ", ".join(missing))

action = source_arm.animation_data.action if source_arm.animation_data else None
if not action:
    raise RuntimeError("Source FBX has no active animation action")
frame_start = int(action.frame_range[0])
frame_end = int(action.frame_range[1])
bpy.context.scene.frame_start = frame_start
bpy.context.scene.frame_end = frame_end

source_height = armature_height(source_arm)
target_height = armature_height(target_arm)
motion_scale = target_height / source_height if source_height else 1.0

target_action = bpy.data.actions.new("HYMotion_Retargeted")
target_arm.animation_data_create()
target_arm.animation_data.action = target_action

target_rest_world = {name: world_rest(target_arm, target_arm.data.bones[name]) for name in target_arm.data.bones.keys()}
source_rest_world = {name: world_rest(source_arm, source_arm.data.bones[name]) for name in source_arm.data.bones.keys()}

root_src = source_arm.pose.bones["Pelvis"]
root_dst = target_arm.pose.bones["mixamorig:Hips"]
root_src_rest = source_rest_world["Pelvis"]
root_dst_rest = target_rest_world["mixamorig:Hips"]
axis_alignment = root_dst_rest.to_quaternion() @ root_src_rest.to_quaternion().inverted()

# HY-Motion is generated on an adult-proportioned body. On a chibi target the
# torso is much wider relative to the arms, so a direct rotation transfer can
# put the wrists inside the body. Add a small symmetrical upper-arm clearance
# bias around the character's forward axis. Ten degrees is enough to clear the
# torso while keeping hand gestures and the generated motion recognizable.
body_up = (
    target_rest_world["mixamorig:Head"].translation
    - target_rest_world["mixamorig:Hips"].translation
).normalized()
body_lateral = (
    target_rest_world["mixamorig:LeftArm"].translation
    - target_rest_world["mixamorig:RightArm"].translation
).normalized()
body_forward = body_lateral.cross(body_up).normalized()
arm_clearance = radians(10.0)

for frame in range(frame_start, frame_end + 1):
    bpy.context.scene.frame_set(frame)
    # Start every frame from the target bind pose. Without this reset, Blender
    # can evaluate a child against its parent's previous-frame matrix and the
    # first few baked frames may contain zero/huge bone scales.
    for pose_bone in target_arm.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()

    desired_world = {}
    root_src_pose = world_pose(source_arm, root_src)
    root_delta_pos = axis_alignment @ ((root_src_pose.translation - root_src_rest.translation) * motion_scale)

    ordered = sorted(
        MAPPING.items(),
        key=lambda item: len(target_arm.data.bones[item[1]].parent_recursive),
    )

    for src_name, dst_name in ordered:
        src_pose_world = world_pose(source_arm, source_arm.pose.bones[src_name])
        src_rest = source_rest_world[src_name]
        dst_rest = target_rest_world[dst_name]

        delta_rot_source = src_pose_world.to_quaternion() @ src_rest.to_quaternion().inverted()
        delta_rot = axis_alignment @ delta_rot_source @ axis_alignment.inverted()
        wanted_rot = delta_rot @ dst_rest.to_quaternion()
        if dst_name == "mixamorig:LeftArm":
            wanted_rot = Quaternion(body_forward, arm_clearance) @ wanted_rot
        elif dst_name == "mixamorig:RightArm":
            wanted_rot = Quaternion(body_forward, -arm_clearance) @ wanted_rot

        dst_bone = target_arm.data.bones[dst_name]
        if dst_name == "mixamorig:Hips":
            wanted_pos = root_dst_rest.translation + root_delta_pos
        elif dst_bone.parent and dst_bone.parent.name in desired_world:
            parent_rest = target_rest_world[dst_bone.parent.name]
            rest_local = parent_rest.inverted() @ dst_rest
            wanted_pos = (desired_world[dst_bone.parent.name] @ Vector((*rest_local.translation, 1.0))).to_3d()
        else:
            wanted_pos = dst_rest.translation

        wanted_world = compose(wanted_pos, wanted_rot)
        desired_world[dst_name] = wanted_world
        pose_bone = target_arm.pose.bones[dst_name]
        pose_bone.matrix = target_arm.matrix_world.inverted() @ wanted_world
        # Refresh the parent/child dependency chain before the next child bone
        # is assigned. This makes frame 1 deterministic instead of converging
        # over several frames.
        bpy.context.view_layer.update()
        pose_bone.rotation_mode = "QUATERNION"
        pose_bone.keyframe_insert("location", frame=frame, group=dst_name)
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame, group=dst_name)
        pose_bone.keyframe_insert("scale", frame=frame, group=dst_name)

for obj in source_objects:
    bpy.data.objects.remove(obj, do_unlink=True)

# HY-Motion's root translation is relative to its source body's rest pose.
# After retargeting to the much shorter chibi rig, that reference can leave the
# visible character floating above world Z=0. Measure the actual deformed mesh
# at the first generated frame and subtract that one constant offset from every
# Hips key. This preserves jumps, steps and forward motion while placing the
# animation's starting floor exactly at zero.
bpy.context.scene.frame_set(frame_start)
bpy.context.view_layer.update()
ground_offset = evaluated_mesh_min_z(target_objects)
hips = target_arm.pose.bones["mixamorig:Hips"]
for frame in range(frame_start, frame_end + 1):
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    hips_world = target_arm.matrix_world @ hips.matrix
    hips_world.translation.z -= ground_offset
    hips.matrix = target_arm.matrix_world.inverted() @ hips_world
    bpy.context.view_layer.update()
    hips.keyframe_insert("location", frame=frame, group="mixamorig:Hips")
print(f"GROUND_OFFSET|{ground_offset:.6f}")

bpy.ops.object.select_all(action="DESELECT")
for obj in target_objects:
    if obj.name in bpy.data.objects:
        obj.select_set(True)
target_arm.select_set(True)
bpy.context.view_layer.objects.active = target_arm

bpy.ops.export_scene.fbx(
    filepath=output_path,
    use_selection=True,
    apply_unit_scale=True,
    apply_scale_options="FBX_SCALE_ALL",
    object_types={"ARMATURE", "MESH", "EMPTY"},
    use_mesh_modifiers=True,
    add_leaf_bones=False,
    use_armature_deform_only=True,
    bake_anim=True,
    bake_anim_use_all_bones=True,
    bake_anim_use_nla_strips=False,
    bake_anim_use_all_actions=False,
    bake_anim_force_startend_keying=True,
    bake_anim_simplify_factor=0.0,
    path_mode="COPY",
    embed_textures=True,
)

if preview_output_path:
    # Keep the deliverable above with its original forward/root motion. For the
    # ComfyUI viewer only, lock the Hips' horizontal world position to frame 1.
    # Load3D otherwise frames the whole travel path and the character starts
    # tiny/off-center with an orbit pivot near the middle of that path.
    hips = target_arm.pose.bones["mixamorig:Hips"]
    bpy.context.scene.frame_set(frame_start)
    bpy.context.view_layer.update()
    first_world = target_arm.matrix_world @ hips.matrix
    anchor_x = first_world.translation.x
    anchor_y = first_world.translation.y

    for frame in range(frame_start, frame_end + 1):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        hips_world = target_arm.matrix_world @ hips.matrix
        hips_world.translation.x = anchor_x
        hips_world.translation.y = anchor_y
        hips.matrix = target_arm.matrix_world.inverted() @ hips_world
        bpy.context.view_layer.update()
        hips.keyframe_insert("location", frame=frame, group="mixamorig:Hips")

    if preview_output_path.lower().endswith(".glb"):
        # GLB preserves the source texture's sRGB appearance in Preview3D.
        # Keep exactly one assigned action so the viewer receives one clip of
        # the correct duration instead of the stale 0.1-second Mixamo actions.
        target_action.name = "Animation"
        target_arm.animation_data.action = target_action
        for pose_bone in target_arm.pose.bones:
            pose_bone.custom_shape = None
        for existing_action in list(bpy.data.actions):
            if existing_action != target_action:
                bpy.data.actions.remove(existing_action)

        bpy.ops.object.select_all(action="DESELECT")
        target_arm.select_set(True)
        for obj in target_objects:
            if obj.type == "MESH" and any(
                modifier.type == "ARMATURE" and modifier.object == target_arm
                for modifier in obj.modifiers
            ):
                obj.select_set(True)
        bpy.context.view_layer.objects.active = target_arm

        print(
            "GLB_SELECTED|"
            + ",".join(f"{obj.name}:{obj.type}" for obj in bpy.context.selected_objects)
        )

        bpy.ops.export_scene.gltf(
            filepath=preview_output_path,
            export_format="GLB",
            use_selection=True,
            export_materials="EXPORT",
            export_image_format="AUTO",
            export_texcoords=True,
            export_normals=True,
            export_skins=True,
            export_animations=True,
            export_animation_mode="ACTIVE_ACTIONS",
            export_frame_range=True,
            export_frame_step=1,
            export_force_sampling=True,
            export_nla_strips=False,
            export_anim_slide_to_zero=True,
            export_cameras=False,
            export_lights=False,
        )
        print(f"PREVIEW_GLB|{preview_output_path}")
    else:
        preview_color_count = prepare_preview_basecolor(preview_output_path, target_objects)
        print(f"PREVIEW_COLOR_COUNT|{preview_color_count}")
        bpy.ops.export_scene.fbx(
            filepath=preview_output_path,
            use_selection=True,
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_ALL",
            object_types={"ARMATURE", "MESH", "EMPTY"},
            use_mesh_modifiers=True,
            add_leaf_bones=False,
            use_armature_deform_only=True,
            bake_anim=True,
            bake_anim_use_all_bones=True,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False,
            bake_anim_force_startend_keying=True,
            bake_anim_simplify_factor=0.0,
            path_mode="COPY",
            embed_textures=True,
        )
        print(f"PREVIEW_FBX|{preview_output_path}")
print(f"RETARGETED|frames={frame_start}-{frame_end}|scale={motion_scale:.6f}|{output_path}")
