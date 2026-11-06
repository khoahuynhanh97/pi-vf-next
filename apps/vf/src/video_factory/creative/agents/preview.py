"""Local preview artifacts; never creates workflow jobs or submits image/video."""
import json
from pathlib import Path

from ..contracts import canonical_digest
from .critic import CreativeCritic
from .strategist import dossier_media


def write_preview(*, dossier, generator, output):
    root = Path(output) / canonical_digest({'dossier':dossier.digest(), 'profile':generator.profile.digest,
        'playbook':generator.recipe['digest'], 'roles':'four-agent-v1'}).removeprefix('sha256:')
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root/'preview_manifest.json'
    cached_creative=None
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding='utf8'))
        if all(canonical_digest(json.loads((root/name).read_text(encoding='utf8'))) == digest
               for name, digest in previous.get('artifacts', {}).items()) and previous.get('artifacts'):
            # Validate source bytes on a cache hit, too.
            for ref in dossier_media(dossier):
                ref.content()
            if previous.get('preproduction_review')!='UNKNOWN':
                return root
            from ..contracts import CreativeBriefV2,ScriptScenePlanV2
            from .contracts import ScenePromptBundle
            cached_creative=(CreativeBriefV2.from_dict(json.loads((root/'brief.json').read_text(encoding='utf8'))),
                ScriptScenePlanV2.from_dict(json.loads((root/'scene_plan.json').read_text(encoding='utf8'))),
                ScenePromptBundle.from_dict(json.loads((root/'scene_prompts.json').read_text(encoding='utf8'))))
    if cached_creative:
        brief,script,bundle=cached_creative
    else:
        brief = generator.create_direction(dossier)
        script = generator.create_script(dossier, brief)
        bundle = generator.create_scene_prompts(dossier, script)
    context = {'dossier':dossier.to_dict(), 'brief':brief.to_dict(), 'script':script.to_dict(),
        'bundle':bundle.to_dict(), 'profile':generator.profile.to_dict(),
        'scene_ids':[s.scene_id for s in script.scenes]}
    review = CreativeCritic(generator.model, receipt_root=root/'reviews').review(
        stage='preproduction', context=context, media=dossier_media(dossier))
    artifacts = {'brief.json':brief.to_dict(), 'scene_plan.json':script.to_dict(),
        'scene_prompts.json':bundle.to_dict(), 'preproduction_review.json':review.to_dict()}
    for name, data in artifacts.items():
        (root/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    manifest = {'schema_version':'creative-preview-v1', 'render_ready':False,
        'preproduction_review':review.verdict, 'dossier_digest':dossier.digest(),
        'profile_digest':generator.profile.digest, 'playbook_digest':generator.recipe['digest'],
        'human_review':'not_performed', 'artifacts':{k:canonical_digest(v) for k,v in artifacts.items()}}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf8')
    return root


def cmd_creative_preview(args):
    from ..contracts import ResearchDossier
    from .contracts import BackendProfile
    from .service import AgenticCreativeGenerator
    from video_factory.handlers.common import create_configured_creative_generator

    dossier = ResearchDossier.from_dict(json.loads(Path(args.dossier).read_text(encoding='utf8')))
    config = json.loads(Path(args.profile).read_text(encoding='utf8'))
    legacy = create_configured_creative_generator(creative_mode='legacy')
    generator = AgenticCreativeGenerator(legacy.model, profile=BackendProfile.from_dict(config['profile']),
        category=config['category'], visible_details=config['visible_details'], source_verifier=legacy.source_verifier)
    print(write_preview(dossier=dossier, generator=generator, output=args.output))
    return 0
