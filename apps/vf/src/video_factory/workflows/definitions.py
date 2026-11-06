from video_factory.workflows.models import WorkflowAggregate

DEFINITIONS = {
    "product_to_video": WorkflowAggregate(
        name="product_to_video",
        initial_state="idle",
        states=["idle", "product_research", "resource_pack_building", "script_generation", "video_production", "completed"],
        transitions={
            "idle": ["product_research"],
            "product_research": ["resource_pack_building"],
            "resource_pack_building": ["script_generation"],
            "script_generation": ["video_production"],
            "video_production": ["completed"],
        },
        required_artifacts={
            "resource_pack_building": ["product_research_report"],
            "script_generation": ["resource_pack"],
            "video_production": ["script"],
            "completed": ["video_file"],
        },
        approval_required={
            "script_generation": True,
            "video_production": True,
        }
    ),
    "product_research": WorkflowAggregate(
        name="product_research",
        initial_state="idle",
        states=["idle", "gathering_data", "analyzing", "completed"],
        transitions={
            "idle": ["gathering_data"],
            "gathering_data": ["analyzing"],
            "analyzing": ["completed"],
        }
    ),
    "knowledge_learning": WorkflowAggregate(
        name="knowledge_learning",
        initial_state="idle",
        states=["idle", "searching", "synthesizing", "completed"],
        transitions={
            "idle": ["searching"],
            "searching": ["synthesizing"],
            "synthesizing": ["completed"],
        }
    ),
    "hook_cta_learning": WorkflowAggregate(
        name="hook_cta_learning",
        initial_state="idle",
        states=["idle", "sampling", "optimizing", "completed"],
        transitions={
            "idle": ["sampling"],
            "sampling": ["optimizing"],
            "optimizing": ["completed"],
        }
    ),
    "video_deconstruction": WorkflowAggregate(
        name="video_deconstruction",
        initial_state="idle",
        states=["idle", "extracting_frames", "analyzing_audio", "generating_report", "completed"],
        transitions={
            "idle": ["extracting_frames", "analyzing_audio"],
            "extracting_frames": ["generating_report"],
            "analyzing_audio": ["generating_report"],
            "generating_report": ["completed"],
        }
    ),
}
