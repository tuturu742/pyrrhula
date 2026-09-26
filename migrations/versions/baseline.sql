--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: action_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.action_record (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    attempt_target character varying(255) NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    server_key character varying(63) NOT NULL,
    tool_name character varying(127) NOT NULL,
    arguments jsonb DEFAULT '{}'::jsonb NOT NULL,
    outcome character varying(16),
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    dispatched_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_action_record_outcome CHECK (((outcome IS NULL) OR ((outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('failed'::character varying)::text, ('reconciled'::character varying)::text]))))
);

ALTER TABLE ONLY public.action_record FORCE ROW LEVEL SECURITY;


--
-- Name: agent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    provider character varying(32) NOT NULL,
    model character varying(255) NOT NULL,
    params jsonb NOT NULL,
    credential_ref character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    fallback_agent_id uuid,
    api_base character varying(500),
    archived_at timestamp with time zone
);

ALTER TABLE ONLY public.agent FORCE ROW LEVEL SECURITY;


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    actor_principal_id uuid NOT NULL,
    action character varying(64) NOT NULL,
    resource_type character varying(32) NOT NULL,
    resource_id uuid,
    target_ids uuid[] NOT NULL,
    query jsonb,
    ip character varying(64),
    user_agent character varying,
    prev_hash character varying(64),
    row_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.audit_log FORCE ROW LEVEL SECURITY;


--
-- Name: await_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.await_state (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    await_kind character varying(32) NOT NULL,
    expected_from jsonb NOT NULL,
    timeout_at timestamp with time zone NOT NULL,
    on_timeout_phase character varying(63) NOT NULL,
    outcome character varying(16),
    satisfied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reminder_at timestamp with time zone,
    CONSTRAINT ck_await_state_outcome CHECK (((outcome IS NULL) OR ((outcome)::text = ANY (ARRAY[('satisfied'::character varying)::text, ('timed_out'::character varying)::text]))))
);

ALTER TABLE ONLY public.await_state FORCE ROW LEVEL SECURITY;


--
-- Name: axis_definition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.axis_definition (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    pack_id character varying(63) NOT NULL,
    key character varying(63) NOT NULL,
    label_key character varying(255) NOT NULL,
    range_min integer NOT NULL,
    range_max integer NOT NULL,
    stakes character varying(8) NOT NULL,
    semantics_md text NOT NULL,
    bindings jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    default_value integer,
    CONSTRAINT ck_axis_definition_range CHECK ((range_min < range_max)),
    CONSTRAINT ck_axis_definition_stakes CHECK (((stakes)::text = ANY (ARRAY[('low'::character varying)::text, ('high'::character varying)::text])))
);

ALTER TABLE ONLY public.axis_definition FORCE ROW LEVEL SECURITY;


--
-- Name: behavior_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.behavior_profile (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    version integer NOT NULL,
    pack_id character varying(63) NOT NULL,
    axis_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.behavior_profile FORCE ROW LEVEL SECURITY;


--
-- Name: checkpoint; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    phase character varying(63) NOT NULL,
    state jsonb NOT NULL,
    actor_cursor jsonb NOT NULL,
    entity_versions jsonb NOT NULL,
    knowledge_version_pins jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.checkpoint FORCE ROW LEVEL SECURITY;


--
-- Name: completed_operation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.completed_operation (
    idempotency_key character varying(255) NOT NULL,
    tenant_id uuid NOT NULL,
    status character varying(16) NOT NULL,
    result jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);

ALTER TABLE ONLY public.completed_operation FORCE ROW LEVEL SECURITY;


--
-- Name: context_manifest; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.context_manifest (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    viewer_principal_id uuid NOT NULL,
    phase character varying(63) NOT NULL,
    entries jsonb DEFAULT '[]'::jsonb NOT NULL,
    redactions jsonb DEFAULT '[]'::jsonb NOT NULL,
    resolution_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    entity_versions jsonb DEFAULT '{}'::jsonb NOT NULL,
    behavior_profile_version integer,
    token_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    rendered_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    history_summary_from_seq integer,
    history_summary_to_seq integer,
    history_summary_hash character varying(64)
);

ALTER TABLE ONLY public.context_manifest FORCE ROW LEVEL SECURITY;


--
-- Name: deployment_setting; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deployment_setting (
    key character varying(64) NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: disclosure_decision; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.disclosure_decision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    persona_id uuid NOT NULL,
    behavior_profile_version integer NOT NULL,
    decisions jsonb NOT NULL,
    agent_id uuid,
    latency_ms integer,
    token_usage jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.disclosure_decision FORCE ROW LEVEL SECURITY;


--
-- Name: entity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    schema_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    scope_key character varying(255) NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    fsm_states jsonb DEFAULT '{}'::jsonb NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    origin_session_id uuid
);

ALTER TABLE ONLY public.entity FORCE ROW LEVEL SECURITY;


--
-- Name: entity_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_schedule (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    kind character varying(8) NOT NULL,
    threshold integer NOT NULL,
    changes jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_entity_schedule_kind CHECK (((kind)::text = ANY (ARRAY[('at'::character varying)::text, ('every'::character varying)::text]))),
    CONSTRAINT ck_entity_schedule_threshold_positive CHECK ((threshold >= 1))
);

ALTER TABLE ONLY public.entity_schedule FORCE ROW LEVEL SECURITY;


--
-- Name: entity_schema; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_schema (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid,
    key character varying(63) NOT NULL,
    version integer NOT NULL,
    fields jsonb NOT NULL,
    derived jsonb DEFAULT '[]'::jsonb NOT NULL,
    state_machines jsonb DEFAULT '[]'::jsonb NOT NULL,
    views jsonb DEFAULT '[]'::jsonb NOT NULL,
    constraints jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    ai_assisted boolean DEFAULT false NOT NULL
);

ALTER TABLE ONLY public.entity_schema FORCE ROW LEVEL SECURITY;


--
-- Name: entity_state_change; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_state_change (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    session_id uuid,
    event_seq integer,
    field_path character varying(255) NOT NULL,
    old_value jsonb,
    new_value jsonb,
    cause character varying(16) NOT NULL,
    cause_ref character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_entity_state_change_cause CHECK (((cause)::text = ANY (ARRAY[('tool'::character varying)::text, ('fsm'::character varying)::text, ('human'::character varying)::text, ('agent'::character varying)::text, ('import'::character varying)::text])))
);

ALTER TABLE ONLY public.entity_state_change FORCE ROW LEVEL SECURITY;


--
-- Name: entry_activation_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entry_activation_state (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    sticky_until_turn integer,
    cooldown_until_turn integer,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.entry_activation_state FORCE ROW LEVEL SECURITY;


--
-- Name: exec_environment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exec_environment (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    engine_key character varying(40),
    image character varying(255) DEFAULT ''::character varying NOT NULL,
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    session_id uuid,
    repo_id uuid,
    spawned_by_persona_id uuid,
    spawned_by_label character varying(120) DEFAULT ''::character varying NOT NULL,
    last_exit_code integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.exec_environment FORCE ROW LEVEL SECURITY;


--
-- Name: identity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    provider character varying(16) NOT NULL,
    external_id character varying(255) NOT NULL,
    email character varying(255),
    password_hash character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.identity FORCE ROW LEVEL SECURITY;


--
-- Name: job; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    kind character varying(64) NOT NULL,
    payload jsonb NOT NULL,
    status character varying(16) NOT NULL,
    attempts integer NOT NULL,
    result jsonb,
    error character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    claimed_at timestamp with time zone,
    completed_at timestamp with time zone
);


--
-- Name: knowledge_chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_chunk (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    version_id uuid,
    ordinal integer NOT NULL,
    text text NOT NULL,
    token_count integer NOT NULL,
    class text NOT NULL,
    scope_key text NOT NULL,
    embedding public.vector(1024),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED,
    content_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding_model text,
    quarantined boolean DEFAULT false NOT NULL
);

ALTER TABLE ONLY public.knowledge_chunk FORCE ROW LEVEL SECURITY;


--
-- Name: knowledge_entry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_entry (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    knowledge_source_id uuid NOT NULL,
    version_id uuid,
    entry_key character varying(255) NOT NULL,
    title character varying(255) NOT NULL,
    body_md text NOT NULL,
    class character varying(32) NOT NULL,
    scope_key character varying(255) NOT NULL,
    keys character varying[] NOT NULL,
    secondary_keys character varying[] NOT NULL,
    logic character varying(8) NOT NULL,
    use_regex boolean NOT NULL,
    constant boolean NOT NULL,
    sticky integer,
    cooldown integer,
    delay integer,
    trigger_pct integer,
    inclusion_group character varying(63),
    "position" character varying(32) NOT NULL,
    insertion_order integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    quarantined boolean DEFAULT false NOT NULL,
    quarantine_reason text,
    quarantine_reviewed_by uuid,
    quarantine_reviewed_at timestamp with time zone,
    CONSTRAINT ck_knowledge_entry_logic CHECK (((logic)::text = ANY (ARRAY[('AND'::character varying)::text, ('OR'::character varying)::text, ('NOT'::character varying)::text])))
);

ALTER TABLE ONLY public.knowledge_entry FORCE ROW LEVEL SECURITY;


--
-- Name: knowledge_source; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_source (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    class character varying(32) NOT NULL,
    owner_principal_id uuid,
    visibility character varying(16) NOT NULL,
    current_version_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT ck_knowledge_source_visibility CHECK (((visibility)::text = ANY (ARRAY[('private'::character varying)::text, ('tenant'::character varying)::text, ('public'::character varying)::text])))
);

ALTER TABLE ONLY public.knowledge_source FORCE ROW LEVEL SECURITY;


--
-- Name: knowledge_source_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_source_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    knowledge_source_id uuid NOT NULL,
    version_number integer NOT NULL,
    content_hash character varying(64) NOT NULL,
    parent_version_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    change_note text,
    ai_assisted boolean DEFAULT false NOT NULL
);

ALTER TABLE ONLY public.knowledge_source_version FORCE ROW LEVEL SECURITY;


--
-- Name: mcp_call_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_call_record (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    server_key character varying(63) NOT NULL,
    tool_name character varying(255) NOT NULL,
    event_seq integer NOT NULL,
    effectful boolean DEFAULT false NOT NULL,
    outcome character varying(16) NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.mcp_call_record FORCE ROW LEVEL SECURITY;


--
-- Name: mcp_server; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_server (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    url character varying(1024) NOT NULL,
    credential_ref character varying(255),
    enabled_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    effectful_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    require_confirmation boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    max_calls_per_session integer,
    timeout_seconds integer,
    max_result_chars integer,
    options jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY public.mcp_server FORCE ROW LEVEL SECURITY;


--
-- Name: membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.membership (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_membership_role CHECK (((role)::text = ANY (ARRAY[('owner'::character varying)::text, ('admin'::character varying)::text, ('editor'::character varying)::text, ('participant'::character varying)::text, ('viewer'::character varying)::text])))
);

ALTER TABLE ONLY public.membership FORCE ROW LEVEL SECURITY;


--
-- Name: message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    author_principal_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    content_md character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    context_manifest_id uuid,
    moderation_flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    citations jsonb DEFAULT '[]'::jsonb NOT NULL,
    resolution_record_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    was_human_override boolean DEFAULT false NOT NULL,
    rewrite_applied boolean DEFAULT false NOT NULL,
    original_content_md text,
    overridden_by_principal_id uuid
);

ALTER TABLE ONLY public.message FORCE ROW LEVEL SECURITY;


--
-- Name: model_capability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_capability (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider character varying(32) NOT NULL,
    model character varying(255) NOT NULL,
    axis_key character varying(63) NOT NULL,
    capable boolean NOT NULL,
    reason text,
    structured_output_fidelity numeric(5,4),
    behavioral_fidelity numeric(5,4),
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: notification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    session_id uuid,
    await_state_id uuid,
    kind character varying(32) NOT NULL,
    dedupe_key character varying(255) NOT NULL,
    subject character varying(255) NOT NULL,
    body_md text NOT NULL,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_notification_kind CHECK (((kind)::text = ANY (ARRAY[('await_opened'::character varying)::text, ('await_reminder'::character varying)::text, ('digest'::character varying)::text])))
);

ALTER TABLE ONLY public.notification FORCE ROW LEVEL SECURITY;


--
-- Name: persona; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    agent_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    persona_type character varying(16) DEFAULT 'participant'::character varying NOT NULL,
    persona_md character varying DEFAULT ''::character varying NOT NULL,
    entity_id uuid,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    archived_at timestamp with time zone,
    web_search boolean DEFAULT false NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_persona_persona_type CHECK (((persona_type)::text = ANY (ARRAY[('supervisor'::character varying)::text, ('participant'::character varying)::text, ('informational'::character varying)::text])))
);

ALTER TABLE ONLY public.persona FORCE ROW LEVEL SECURITY;


--
-- Name: persona_git_credential; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona_git_credential (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    repo_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    credential_ref uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE ONLY public.persona_git_credential FORCE ROW LEVEL SECURITY;


--
-- Name: persona_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    persona_md text NOT NULL,
    created_by uuid,
    ai_assisted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.persona_version FORCE ROW LEVEL SECURITY;


--
-- Name: plugin_repository; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plugin_repository (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(63) NOT NULL,
    url character varying(1023) NOT NULL,
    ref character varying(255) NOT NULL,
    source character varying(16) DEFAULT 'git'::character varying NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    last_error text DEFAULT ''::text NOT NULL,
    workflow_keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: preview_environment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.preview_environment (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    ref character varying(120) DEFAULT ''::character varying NOT NULL,
    engine_key character varying(40),
    image character varying(255) DEFAULT ''::character varying NOT NULL,
    internal_url character varying(255) DEFAULT ''::character varying NOT NULL,
    status character varying(16) DEFAULT 'starting'::character varying NOT NULL,
    workspace_id uuid,
    session_id uuid,
    repo_id uuid,
    artifact_name character varying(120) DEFAULT ''::character varying NOT NULL,
    created_by_principal_id uuid,
    created_by_label character varying(120) DEFAULT ''::character varying NOT NULL,
    expires_at timestamp with time zone,
    last_error text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    git_ref character varying(255) DEFAULT ''::character varying NOT NULL
);

ALTER TABLE ONLY public.preview_environment FORCE ROW LEVEL SECURITY;


--
-- Name: price_table; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.price_table (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider character varying(32) NOT NULL,
    model character varying(255) NOT NULL,
    effective_from date NOT NULL,
    input_per_mtok numeric(12,6) NOT NULL,
    output_per_mtok numeric(12,6) NOT NULL
);


--
-- Name: principal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.principal (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    kind character varying(16) NOT NULL,
    display_name character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    disabled_at timestamp with time zone,
    CONSTRAINT ck_principal_kind CHECK (((kind)::text = ANY (ARRAY[('human'::character varying)::text, ('service'::character varying)::text, ('agent'::character varying)::text])))
);

ALTER TABLE ONLY public.principal FORCE ROW LEVEL SECURITY;


--
-- Name: process_definition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.process_definition (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid,
    key character varying(63) NOT NULL,
    version integer NOT NULL,
    name character varying(255) NOT NULL,
    definition jsonb NOT NULL,
    validated_at timestamp with time zone,
    validation_errors jsonb,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone
);

ALTER TABLE ONLY public.process_definition FORCE ROW LEVEL SECURITY;


--
-- Name: provider_credential; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_credential (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    ciphertext character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.provider_credential FORCE ROW LEVEL SECURITY;


--
-- Name: registration_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registration_request (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    display_name character varying(255) NOT NULL,
    password_hash text NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    decided_by_principal_id uuid,
    CONSTRAINT ck_registration_request_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[])))
);

ALTER TABLE ONLY public.registration_request FORCE ROW LEVEL SECURITY;


--
-- Name: repo; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.repo (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    source_url character varying(1023),
    credential_ref uuid,
    runtime character varying(31) DEFAULT 'debian'::character varying NOT NULL,
    setup_cmds jsonb DEFAULT '[]'::jsonb NOT NULL,
    test_cmd character varying(511),
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    provider character varying(16),
    runtime_image character varying(255),
    registry_credential_ref uuid,
    build_cmd character varying(511),
    artifact_name character varying(255),
    preview_image character varying(255),
    preview_cmd character varying(2000),
    preview_port integer,
    preview_env jsonb DEFAULT '{}'::jsonb NOT NULL,
    default_branch character varying(255) DEFAULT 'main'::character varying NOT NULL
);

ALTER TABLE ONLY public.repo FORCE ROW LEVEL SECURITY;


--
-- Name: report; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    template_key character varying(63) NOT NULL,
    audience_mode character varying(16) NOT NULL,
    generated_for_principal_id uuid NOT NULL,
    source_event_from integer NOT NULL,
    source_event_to integer NOT NULL,
    source_manifest_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    content_md text NOT NULL,
    redactions jsonb DEFAULT '[]'::jsonb NOT NULL,
    artifacts jsonb DEFAULT '{}'::jsonb NOT NULL,
    fact_frame_hash character varying(64) NOT NULL,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_report_audience_mode CHECK (((audience_mode)::text = ANY (ARRAY[('participant'::character varying)::text, ('overseer'::character varying)::text, ('sanitised'::character varying)::text])))
);

ALTER TABLE ONLY public.report FORCE ROW LEVEL SECURITY;


--
-- Name: resolution_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resolution_record (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    tool_key character varying(63) NOT NULL,
    actor_entity_id uuid,
    expression character varying(255) NOT NULL,
    seed character varying(64) NOT NULL,
    rolls jsonb NOT NULL,
    modifiers jsonb NOT NULL,
    total integer NOT NULL,
    target integer,
    outcome character varying(32) NOT NULL,
    rule_system_id uuid NOT NULL,
    rule_citation_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    prev_hash character varying(64),
    row_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.resolution_record FORCE ROW LEVEL SECURITY;


--
-- Name: role_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permission (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    role character varying(32) NOT NULL,
    action character varying(64) NOT NULL,
    resource_type character varying(32) NOT NULL
);


--
-- Name: rule_system; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rule_system (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    expression_grammar jsonb NOT NULL,
    check_types jsonb NOT NULL,
    outcome_bands jsonb DEFAULT '[]'::jsonb NOT NULL,
    modifier_resolver jsonb NOT NULL,
    validators jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.rule_system FORCE ROW LEVEL SECURITY;


--
-- Name: scope; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scope (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    key character varying(255) NOT NULL,
    kind character varying(16) NOT NULL,
    members jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_scope_kind CHECK (((kind)::text = ANY (ARRAY[('public'::character varying)::text, ('role'::character varying)::text, ('group'::character varying)::text, ('private'::character varying)::text])))
);

ALTER TABLE ONLY public.scope FORCE ROW LEVEL SECURITY;


--
-- Name: secret; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secret (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    subject_kind character varying(16) NOT NULL,
    subject_id uuid NOT NULL,
    content_ciphertext text NOT NULL,
    gist text NOT NULL,
    hint_text text,
    behavioral_directive text,
    disclosure_state character varying(16) NOT NULL,
    scope_key character varying(255) NOT NULL,
    authored_by uuid,
    version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    gist_embedding public.vector(1024),
    publication character varying(16) DEFAULT 'guarded'::character varying NOT NULL,
    CONSTRAINT ck_secret_disclosure_state CHECK (((disclosure_state)::text = ANY (ARRAY[('undisclosed'::character varying)::text, ('hinted'::character varying)::text, ('partial'::character varying)::text, ('public'::character varying)::text]))),
    CONSTRAINT ck_secret_publication CHECK (((publication)::text = ANY (ARRAY[('guarded'::character varying)::text, ('publishable'::character varying)::text]))),
    CONSTRAINT ck_secret_subject_kind CHECK (((subject_kind)::text = ANY (ARRAY[('entity'::character varying)::text, ('agent'::character varying)::text, ('workspace'::character varying)::text, ('knowledge_entry'::character varying)::text])))
);

ALTER TABLE ONLY public.secret FORCE ROW LEVEL SECURITY;


--
-- Name: secret_disclosure_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secret_disclosure_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    secret_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    disclosed_by_principal_id uuid,
    disclosed_to jsonb NOT NULL,
    mode character varying(16) NOT NULL,
    decision_id uuid,
    message_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_secret_disclosure_event_mode CHECK (((mode)::text = ANY (ARRAY[('full'::character varying)::text, ('hint'::character varying)::text, ('inferred'::character varying)::text, ('leaked'::character varying)::text])))
);

ALTER TABLE ONLY public.secret_disclosure_event FORCE ROW LEVEL SECURITY;


--
-- Name: secret_holder; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secret_holder (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    secret_id uuid NOT NULL,
    holder_principal_id uuid NOT NULL,
    holder_kind character varying(16) NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    acquired_via_event_id uuid,
    CONSTRAINT ck_secret_holder_kind CHECK (((holder_kind)::text = ANY (ARRAY[('author'::character varying)::text, ('discovered'::character varying)::text, ('told'::character varying)::text])))
);

ALTER TABLE ONLY public.secret_holder FORCE ROW LEVEL SECURITY;


--
-- Name: session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    current_phase character varying(63) NOT NULL,
    status character varying(16) NOT NULL,
    next_event_seq integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    process_definition_id uuid,
    process_definition_version integer,
    actor_cursor jsonb DEFAULT '{}'::jsonb NOT NULL,
    forked_from_checkpoint_id uuid,
    version integer DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone,
    claimed_by character varying(64),
    roll_secret character varying(64),
    archived_at timestamp with time zone,
    agenda_md text,
    turn_policy character varying(16) DEFAULT 'auto'::character varying NOT NULL,
    name character varying(255),
    awaiting character varying(16)
);

ALTER TABLE ONLY public.session FORCE ROW LEVEL SECURITY;


--
-- Name: session_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_seq integer NOT NULL,
    kind character varying(32) NOT NULL,
    payload jsonb NOT NULL,
    actor_principal_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.session_event FORCE ROW LEVEL SECURITY;


--
-- Name: session_persona; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_persona (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    is_supervisor boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.session_persona FORCE ROW LEVEL SECURITY;


--
-- Name: session_repo; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_repo (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    repo_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.session_repo FORCE ROW LEVEL SECURITY;


--
-- Name: tenant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    isolation_mode character varying(16) NOT NULL,
    region character varying(63) NOT NULL,
    settings jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_library boolean DEFAULT false NOT NULL,
    deactivated_at timestamp with time zone,
    CONSTRAINT ck_tenant_isolation_mode CHECK (((isolation_mode)::text = ANY (ARRAY[('shared'::character varying)::text, ('schema'::character varying)::text, ('database'::character varying)::text])))
);


--
-- Name: tenant_mcp_capability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_mcp_capability (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    url character varying(1024) NOT NULL,
    credential_ref character varying(255),
    enabled_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    effectful_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    require_confirmation boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.tenant_mcp_capability FORCE ROW LEVEL SECURITY;


--
-- Name: tool_definition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_definition (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    kind character varying(16) NOT NULL,
    input_schema jsonb NOT NULL,
    output_schema jsonb NOT NULL,
    impl_ref character varying(255) NOT NULL,
    validation_ref character varying(63),
    determinism character varying(16) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.tool_definition FORCE ROW LEVEL SECURITY;


--
-- Name: usage_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_record (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid,
    session_id uuid,
    persona_id uuid,
    agent_id uuid,
    provider character varying(32) NOT NULL,
    model character varying(255) NOT NULL,
    phase character varying(32),
    purpose character varying(16) NOT NULL,
    prompt_tokens integer NOT NULL,
    completion_tokens integer NOT NULL,
    cached_tokens integer NOT NULL,
    estimated_cost numeric(12,6) NOT NULL,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    message_id uuid,
    principal_id uuid
);

ALTER TABLE ONLY public.usage_record FORCE ROW LEVEL SECURITY;


--
-- Name: vector_store_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vector_store_item (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    scope_key text NOT NULL,
    class text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector(8) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.vector_store_item FORCE ROW LEVEL SECURITY;


--
-- Name: vocabulary_overlay; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vocabulary_overlay (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    labels jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.vocabulary_overlay FORCE ROW LEVEL SECURITY;


--
-- Name: workflow; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    overlay_key character varying(63),
    persona_type_labels jsonb NOT NULL,
    capabilities jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid,
    label_overrides jsonb DEFAULT '{}'::jsonb NOT NULL,
    featured_process_keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_by uuid
);

ALTER TABLE ONLY public.workflow FORCE ROW LEVEL SECURITY;


--
-- Name: workspace; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    key character varying(63) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying,
    settings jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    vocabulary_overlay_id uuid,
    clock_value integer DEFAULT 0 NOT NULL,
    clock_advanced_at timestamp with time zone
);

ALTER TABLE ONLY public.workspace FORCE ROW LEVEL SECURITY;


--
-- Name: workspace_knowledge_attachment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_knowledge_attachment (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    knowledge_source_id uuid NOT NULL,
    version_pin uuid,
    scope_key character varying(255) NOT NULL,
    priority_weight numeric(6,4) NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.workspace_knowledge_attachment FORCE ROW LEVEL SECURITY;


--
-- Name: workspace_membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_membership (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_membership_role CHECK (((role)::text = ANY (ARRAY[('steward'::character varying)::text, ('facilitator'::character varying)::text, ('participant'::character varying)::text, ('overseer'::character varying)::text, ('viewer'::character varying)::text])))
);

ALTER TABLE ONLY public.workspace_membership FORCE ROW LEVEL SECURITY;


--
-- Data for Name: action_record; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: agent; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: audit_log; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: await_state; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: axis_definition; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: behavior_profile; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: checkpoint; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: completed_operation; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: context_manifest; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: deployment_setting; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: disclosure_decision; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity_schedule; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity_schema; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entity_state_change; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: entry_activation_state; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: exec_environment; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: identity; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: job; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: knowledge_chunk; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: knowledge_entry; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: knowledge_source; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: knowledge_source_version; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: mcp_call_record; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: mcp_server; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: membership; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: message; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: model_capability; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: notification; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: persona; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: persona_git_credential; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: persona_version; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: plugin_repository; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: preview_environment; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: price_table; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.price_table VALUES ('ba9ec32d-61a5-4e9b-aea0-320c4a68d7d4', 'ollama', '*', '2026-01-01', 0.000000, 0.000000);
INSERT INTO public.price_table VALUES ('fa868e99-20a0-4499-842f-92f9de17f50f', 'echo', '*', '2026-01-01', 0.000000, 0.000000);
INSERT INTO public.price_table VALUES ('39e22cec-76fe-436a-8990-d53a318612a8', '*', '*', '2026-01-01', 0.000000, 0.000000);


--
-- Data for Name: principal; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: process_definition; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: provider_credential; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: registration_request; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: repo; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: report; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: resolution_record; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: role_permission; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.role_permission VALUES ('2966aeb6-fb51-46e5-b690-5900ca2772d9', 'owner', 'manage_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('3789d397-a52f-4f2c-9853-31104773f798', 'owner', 'manage_members', 'tenant');
INSERT INTO public.role_permission VALUES ('93dbbd48-c9d7-4957-bde0-59969ab5ccf5', 'owner', 'manage_billing', 'tenant');
INSERT INTO public.role_permission VALUES ('7e0b0f68-814a-47db-ab6c-4d6b533514e0', 'owner', 'view_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('9adca671-965b-4c50-8184-e6496d0fe0d3', 'admin', 'manage_members', 'tenant');
INSERT INTO public.role_permission VALUES ('29bb49db-507d-4841-974c-7b8d51326b92', 'admin', 'view_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('14944ae8-16d9-47ce-b01d-2acb0fa17c07', 'editor', 'view_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('37995e71-e98f-4e92-adb1-3b2858b48705', 'participant', 'view_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('c15518e1-b3c9-4237-adc4-cf38dab9fe07', 'viewer', 'view_tenant', 'tenant');
INSERT INTO public.role_permission VALUES ('84d6a0d7-21a6-4a50-9370-7aa98bd71e71', 'facilitator', 'manage_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('27a716bc-ffaf-4640-b973-3d1c5bf766fa', 'facilitator', 'manage_process', 'workspace');
INSERT INTO public.role_permission VALUES ('b6546e80-80f4-4f34-9ddc-a4b3a501d014', 'facilitator', 'manage_knowledge', 'workspace');
INSERT INTO public.role_permission VALUES ('854bab1c-f416-4ca2-b8b9-82c6250f9f3f', 'facilitator', 'act_in_session', 'workspace');
INSERT INTO public.role_permission VALUES ('2dd6d657-d939-46a1-ade2-398ddbeea2b6', 'facilitator', 'view_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('636a8333-b6cb-4e78-a9b8-3f7508fd61cb', 'participant', 'act_in_session', 'workspace');
INSERT INTO public.role_permission VALUES ('2e7bfe57-5fa2-4817-82f7-abb5086b6e09', 'participant', 'view_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('2ca29cf0-928e-4d28-9e53-de705af1cf44', 'overseer', 'secret:inspect', 'workspace');
INSERT INTO public.role_permission VALUES ('7a3d6cff-80a7-4410-b500-516db034cdb9', 'overseer', 'view_audit', 'workspace');
INSERT INTO public.role_permission VALUES ('db5abfae-7d38-44cf-bf7e-0ceb5a39b9ed', 'overseer', 'view_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('baef3f55-5197-423f-b19b-8169ef7a1f45', 'viewer', 'view_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('6b93e6d4-2353-4f1b-ac03-9b6e76ee5227', 'facilitator', 'read_any_manifest', 'workspace');
INSERT INTO public.role_permission VALUES ('98e1417a-85eb-436b-ac57-c517d654429e', 'overseer', 'read_any_manifest', 'workspace');
INSERT INTO public.role_permission VALUES ('0e448113-e464-434f-82d8-fc811ffa576d', 'facilitator', 'secret:author', 'workspace');
INSERT INTO public.role_permission VALUES ('334b8225-ea4b-4c6b-a68c-9dfdf9fb646a', 'facilitator', 'entity:mutate', 'workspace');
INSERT INTO public.role_permission VALUES ('32ec778f-39a5-4d91-a43e-e5fba05ddc7e', 'participant', 'entity:mutate', 'workspace');
INSERT INTO public.role_permission VALUES ('25a2d2b8-8c17-464f-9f16-3de188660fbe', 'facilitator', 'workspace:advance_clock', 'workspace');
INSERT INTO public.role_permission VALUES ('b18c601a-a023-4f85-bfd5-15afb8b36696', 'facilitator', 'agent:speak_as', 'workspace');
INSERT INTO public.role_permission VALUES ('a95c325a-9cb2-40a8-8713-daa01d92ff75', 'overseer', 'agent:speak_as', 'workspace');
INSERT INTO public.role_permission VALUES ('e03a88e1-c9c4-4e79-9772-2237e249bfde', 'owner', 'session:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('ac1f630f-8430-46b1-a8d3-b878a9fd1817', 'admin', 'session:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('2b736c67-585f-43e0-b54b-a188a2ffeef1', 'editor', 'session:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('a2b3d3b1-1b5b-4f00-88fe-28cec75ec426', 'owner', 'knowledge:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('f08c853d-cae7-4522-a388-ee69668699b3', 'admin', 'knowledge:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('40cf4f6a-8c6e-4548-91fc-14d9ebdc3cf9', 'editor', 'knowledge:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('34e7c604-3c34-42af-af89-99551c223411', 'owner', 'process_definition:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('1afe1f4a-5474-41ea-969f-86cdf634c9b1', 'admin', 'process_definition:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('3924e1c0-35f5-40b5-833c-badba687cbda', 'editor', 'process_definition:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('2312a690-4691-4b90-ab27-b8fe0278af15', 'owner', 'persona:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('44cea024-146d-48c3-971e-b9b933ffa2d5', 'admin', 'persona:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('44e9277c-799b-4d3c-a89e-620458b4a499', 'editor', 'persona:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('3d47e31d-3a61-46ab-b96a-b4954bf7d3b3', 'owner', 'agent:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('3560ab8b-bdf0-48a0-8f21-99c7c228feb1', 'admin', 'agent:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('7f7a3a8a-2f08-437b-849a-a1e1a0b50ffa', 'editor', 'agent:archive', 'tenant');
INSERT INTO public.role_permission VALUES ('4144dfea-a878-4041-8027-4c6fc5101824', 'facilitator', 'session:conduct', 'workspace');
INSERT INTO public.role_permission VALUES ('b1652f63-332b-489e-9792-369ed044c5a8', 'overseer', 'session:conduct', 'workspace');
INSERT INTO public.role_permission VALUES ('1411c9db-9170-4a8d-ac44-4a70dc5b5a6f', 'facilitator', 'entity:create', 'workspace');
INSERT INTO public.role_permission VALUES ('f7ece6cf-027b-453c-9598-9add28d3b861', 'participant', 'entity:create', 'workspace');
INSERT INTO public.role_permission VALUES ('054a7814-ea19-4140-9945-c0af0350cdb7', 'owner', 'workflow:manage', 'tenant');
INSERT INTO public.role_permission VALUES ('d3212142-5d24-40f8-bb9e-f02e768d413d', 'admin', 'workflow:manage', 'tenant');
INSERT INTO public.role_permission VALUES ('6872acf6-b606-4c97-a991-a6ac04cf53c6', 'owner', 'repo:manage', 'tenant');
INSERT INTO public.role_permission VALUES ('dc031465-3ae7-4853-a1e2-e9788a6dcb01', 'admin', 'repo:manage', 'tenant');
INSERT INTO public.role_permission VALUES ('f01df3f4-6e79-4465-b199-c81247e318e7', 'overseer', 'manage_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('8036bf02-1c94-4d8f-bdcd-815b6f69aa08', 'steward', 'act_in_session', 'workspace');
INSERT INTO public.role_permission VALUES ('cbcc098b-567c-42d0-96f7-669501bc9856', 'steward', 'agent:speak_as', 'workspace');
INSERT INTO public.role_permission VALUES ('825f647b-a409-41f2-a2aa-870d0c346d7b', 'steward', 'entity:create', 'workspace');
INSERT INTO public.role_permission VALUES ('e0501abc-c268-445b-834b-f617038e1dd3', 'steward', 'entity:mutate', 'workspace');
INSERT INTO public.role_permission VALUES ('552b1589-9e0d-42b7-86bf-78a94331a9f9', 'steward', 'manage_knowledge', 'workspace');
INSERT INTO public.role_permission VALUES ('02fa86c1-f18e-4294-b69f-c377d4bee3c5', 'steward', 'manage_process', 'workspace');
INSERT INTO public.role_permission VALUES ('84efe2bd-efd7-4325-a234-7f12c2a70784', 'steward', 'manage_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('b475073c-5a8e-4f76-b638-a1cc337c5d01', 'steward', 'read_any_manifest', 'workspace');
INSERT INTO public.role_permission VALUES ('589d0ade-6f73-40e2-8615-80078627a8de', 'steward', 'secret:author', 'workspace');
INSERT INTO public.role_permission VALUES ('602cdda6-059c-4cbb-8fa6-b53c8bea5f20', 'steward', 'secret:inspect', 'workspace');
INSERT INTO public.role_permission VALUES ('1d922bfc-19d5-4686-99ca-a54120ef03a5', 'steward', 'session:conduct', 'workspace');
INSERT INTO public.role_permission VALUES ('101791c8-8d61-4e07-9353-e365eabaeaff', 'steward', 'view_audit', 'workspace');
INSERT INTO public.role_permission VALUES ('7117b741-d078-47b0-8a50-870c5fce2ff3', 'steward', 'view_workspace', 'workspace');
INSERT INTO public.role_permission VALUES ('8acec54d-ee00-4517-8459-877d533e624f', 'steward', 'workspace:advance_clock', 'workspace');


--
-- Data for Name: rule_system; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: scope; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: secret; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: secret_disclosure_event; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: secret_holder; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: session; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: session_event; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: session_persona; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: session_repo; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: tenant; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.tenant VALUES ('00000000-0000-0000-0000-000000000001', 'pyrrhula-library', 'Pyrrhula Library', 'shared', 'default', '{}', '2026-09-09 20:49:27.183958+00', true, NULL);
INSERT INTO public.tenant VALUES ('00000000-0000-0000-0000-000000000002', 'admin', 'Platform Admin', 'shared', 'default', '{"system_admin": true}', '2026-09-09 20:49:27.183958+00', false, NULL);


--
-- Data for Name: tenant_mcp_capability; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: tool_definition; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: usage_record; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: vector_store_item; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: vocabulary_overlay; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: workflow; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.workflow VALUES ('b06ffb7f-3515-4375-8374-4c37e5485ffa', 'rpg', 'Tabletop RPG', 'rpg_v1', '{"supervisor": "Game Master", "participant": "Player"}', '{"mcp_servers": []}', '2026-09-09 20:49:27.183958+00', NULL, '{}', '[]', NULL);
INSERT INTO public.workflow VALUES ('0ed33f40-5a1e-4aaf-a3ee-180ced9d94d2', 'swdev', 'Software Development', 'swdev_v1', '{"supervisor": "Lead", "participant": "Engineer"}', '{"mcp_servers": [{"key": "git", "url": "http://mcp-git:8080", "enabled_tools": ["git_status", "git_log", "git_diff", "git_show", "git_commit"], "effectful_tools": ["git_commit"]}]}', '2026-09-09 20:49:27.183958+00', NULL, '{}', '[]', NULL);
INSERT INTO public.workflow VALUES ('27e5c5c6-fcf8-4c40-b6d1-7e42fdf7b161', 'default', 'Default', 'default_v1', '{"supervisor": "Facilitator", "participant": "Contributor"}', '{"mcp_servers": []}', '2026-09-09 20:49:27.183958+00', NULL, '{}', '[]', NULL);


--
-- Data for Name: workspace; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: workspace_knowledge_attachment; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: workspace_membership; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Name: action_record action_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_record
    ADD CONSTRAINT action_record_pkey PRIMARY KEY (id);


--
-- Name: persona_version agent_persona_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_version
    ADD CONSTRAINT agent_persona_version_pkey PRIMARY KEY (id);


--
-- Name: persona agent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona
    ADD CONSTRAINT agent_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: await_state await_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.await_state
    ADD CONSTRAINT await_state_pkey PRIMARY KEY (id);


--
-- Name: axis_definition axis_definition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.axis_definition
    ADD CONSTRAINT axis_definition_pkey PRIMARY KEY (id);


--
-- Name: behavior_profile behavior_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavior_profile
    ADD CONSTRAINT behavior_profile_pkey PRIMARY KEY (id);


--
-- Name: checkpoint checkpoint_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint
    ADD CONSTRAINT checkpoint_pkey PRIMARY KEY (id);


--
-- Name: completed_operation completed_operation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.completed_operation
    ADD CONSTRAINT completed_operation_pkey PRIMARY KEY (idempotency_key);


--
-- Name: context_manifest context_manifest_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_manifest
    ADD CONSTRAINT context_manifest_pkey PRIMARY KEY (id);


--
-- Name: deployment_setting deployment_setting_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deployment_setting
    ADD CONSTRAINT deployment_setting_pkey PRIMARY KEY (key);


--
-- Name: disclosure_decision disclosure_decision_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_decision
    ADD CONSTRAINT disclosure_decision_pkey PRIMARY KEY (id);


--
-- Name: entity entity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_pkey PRIMARY KEY (id);


--
-- Name: entity_schedule entity_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT entity_schedule_pkey PRIMARY KEY (id);


--
-- Name: entity_schema entity_schema_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schema
    ADD CONSTRAINT entity_schema_pkey PRIMARY KEY (id);


--
-- Name: entity_state_change entity_state_change_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_state_change
    ADD CONSTRAINT entity_state_change_pkey PRIMARY KEY (id);


--
-- Name: entry_activation_state entry_activation_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entry_activation_state
    ADD CONSTRAINT entry_activation_state_pkey PRIMARY KEY (id);


--
-- Name: exec_environment exec_environment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exec_environment
    ADD CONSTRAINT exec_environment_pkey PRIMARY KEY (id);


--
-- Name: identity identity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity
    ADD CONSTRAINT identity_pkey PRIMARY KEY (id);


--
-- Name: job job_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job
    ADD CONSTRAINT job_pkey PRIMARY KEY (id);


--
-- Name: knowledge_chunk knowledge_chunk_entry_id_ordinal_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_entry_id_ordinal_key UNIQUE (entry_id, ordinal);


--
-- Name: knowledge_chunk knowledge_chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_pkey PRIMARY KEY (id);


--
-- Name: knowledge_entry knowledge_entry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_entry
    ADD CONSTRAINT knowledge_entry_pkey PRIMARY KEY (id);


--
-- Name: knowledge_source knowledge_source_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source
    ADD CONSTRAINT knowledge_source_pkey PRIMARY KEY (id);


--
-- Name: knowledge_source_version knowledge_source_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT knowledge_source_version_pkey PRIMARY KEY (id);


--
-- Name: mcp_call_record mcp_call_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_call_record
    ADD CONSTRAINT mcp_call_record_pkey PRIMARY KEY (id);


--
-- Name: mcp_server mcp_server_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server
    ADD CONSTRAINT mcp_server_pkey PRIMARY KEY (id);


--
-- Name: membership membership_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_pkey PRIMARY KEY (id);


--
-- Name: message message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message
    ADD CONSTRAINT message_pkey PRIMARY KEY (id);


--
-- Name: model_capability model_capability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_capability
    ADD CONSTRAINT model_capability_pkey PRIMARY KEY (id);


--
-- Name: agent model_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT model_profile_pkey PRIMARY KEY (id);


--
-- Name: notification notification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_pkey PRIMARY KEY (id);


--
-- Name: persona_git_credential persona_git_credential_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_git_credential
    ADD CONSTRAINT persona_git_credential_pkey PRIMARY KEY (id);


--
-- Name: plugin_repository plugin_repository_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plugin_repository
    ADD CONSTRAINT plugin_repository_pkey PRIMARY KEY (id);


--
-- Name: preview_environment preview_environment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preview_environment
    ADD CONSTRAINT preview_environment_pkey PRIMARY KEY (id);


--
-- Name: price_table price_table_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_table
    ADD CONSTRAINT price_table_pkey PRIMARY KEY (id);


--
-- Name: principal principal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.principal
    ADD CONSTRAINT principal_pkey PRIMARY KEY (id);


--
-- Name: process_definition process_definition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_definition
    ADD CONSTRAINT process_definition_pkey PRIMARY KEY (id);


--
-- Name: provider_credential provider_credential_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_credential
    ADD CONSTRAINT provider_credential_pkey PRIMARY KEY (id);


--
-- Name: registration_request registration_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registration_request
    ADD CONSTRAINT registration_request_pkey PRIMARY KEY (id);


--
-- Name: repo repo_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.repo
    ADD CONSTRAINT repo_pkey PRIMARY KEY (id);


--
-- Name: report report_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report
    ADD CONSTRAINT report_pkey PRIMARY KEY (id);


--
-- Name: resolution_record resolution_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resolution_record
    ADD CONSTRAINT resolution_record_pkey PRIMARY KEY (id);


--
-- Name: role_permission role_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT role_permission_pkey PRIMARY KEY (id);


--
-- Name: rule_system rule_system_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_system
    ADD CONSTRAINT rule_system_pkey PRIMARY KEY (id);


--
-- Name: scope scope_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope
    ADD CONSTRAINT scope_pkey PRIMARY KEY (id);


--
-- Name: secret_disclosure_event secret_disclosure_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_pkey PRIMARY KEY (id);


--
-- Name: secret_holder secret_holder_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT secret_holder_pkey PRIMARY KEY (id);


--
-- Name: secret secret_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret
    ADD CONSTRAINT secret_pkey PRIMARY KEY (id);


--
-- Name: session_event session_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_event
    ADD CONSTRAINT session_event_pkey PRIMARY KEY (id);


--
-- Name: session_persona session_persona_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_persona
    ADD CONSTRAINT session_persona_pkey PRIMARY KEY (id);


--
-- Name: session session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_pkey PRIMARY KEY (id);


--
-- Name: session_repo session_repo_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_repo
    ADD CONSTRAINT session_repo_pkey PRIMARY KEY (id);


--
-- Name: tenant_mcp_capability tenant_mcp_capability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_mcp_capability
    ADD CONSTRAINT tenant_mcp_capability_pkey PRIMARY KEY (id);


--
-- Name: tenant tenant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant
    ADD CONSTRAINT tenant_pkey PRIMARY KEY (id);


--
-- Name: tenant tenant_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant
    ADD CONSTRAINT tenant_slug_key UNIQUE (slug);


--
-- Name: tool_definition tool_definition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_definition
    ADD CONSTRAINT tool_definition_pkey PRIMARY KEY (id);


--
-- Name: action_record uq_action_record_tenant_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_record
    ADD CONSTRAINT uq_action_record_tenant_idempotency UNIQUE (tenant_id, idempotency_key);


--
-- Name: axis_definition uq_axis_definition_tenant_pack_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.axis_definition
    ADD CONSTRAINT uq_axis_definition_tenant_pack_key UNIQUE (tenant_id, pack_id, key);


--
-- Name: behavior_profile uq_behavior_profile_agent_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavior_profile
    ADD CONSTRAINT uq_behavior_profile_agent_version UNIQUE (persona_id, version);


--
-- Name: checkpoint uq_checkpoint_session_event_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint
    ADD CONSTRAINT uq_checkpoint_session_event_seq UNIQUE (session_id, event_seq);


--
-- Name: context_manifest uq_context_manifest_session_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_manifest
    ADD CONSTRAINT uq_context_manifest_session_seq UNIQUE (session_id, event_seq);


--
-- Name: entity_schedule uq_entity_schedule_workspace_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT uq_entity_schedule_workspace_key UNIQUE (workspace_id, key);


--
-- Name: entity_schema uq_entity_schema_tenant_ws_key_ver; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schema
    ADD CONSTRAINT uq_entity_schema_tenant_ws_key_ver UNIQUE (tenant_id, workspace_id, key, version);


--
-- Name: entity uq_entity_tenant_ws_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT uq_entity_tenant_ws_key UNIQUE (tenant_id, workspace_id, key);


--
-- Name: entry_activation_state uq_entry_activation_state; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entry_activation_state
    ADD CONSTRAINT uq_entry_activation_state UNIQUE (session_id, entry_id);


--
-- Name: exec_environment uq_exec_environment_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exec_environment
    ADD CONSTRAINT uq_exec_environment_name UNIQUE (tenant_id, name);


--
-- Name: identity uq_identity_tenant_provider_ext; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity
    ADD CONSTRAINT uq_identity_tenant_provider_ext UNIQUE (tenant_id, provider, external_id);


--
-- Name: knowledge_source uq_knowledge_source_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source
    ADD CONSTRAINT uq_knowledge_source_tenant_key UNIQUE (tenant_id, key);


--
-- Name: knowledge_source_version uq_knowledge_source_version_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT uq_knowledge_source_version_number UNIQUE (knowledge_source_id, version_number);


--
-- Name: mcp_server uq_mcp_server_workspace_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server
    ADD CONSTRAINT uq_mcp_server_workspace_key UNIQUE (workspace_id, key);


--
-- Name: membership uq_membership_tenant_principal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT uq_membership_tenant_principal UNIQUE (tenant_id, principal_id);


--
-- Name: model_capability uq_model_capability; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_capability
    ADD CONSTRAINT uq_model_capability UNIQUE (provider, model, axis_key);


--
-- Name: notification uq_notification_tenant_dedupe; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT uq_notification_tenant_dedupe UNIQUE (tenant_id, dedupe_key);


--
-- Name: persona_git_credential uq_persona_git_credential_repo_persona; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_git_credential
    ADD CONSTRAINT uq_persona_git_credential_repo_persona UNIQUE (repo_id, persona_id);


--
-- Name: plugin_repository uq_plugin_repository_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plugin_repository
    ADD CONSTRAINT uq_plugin_repository_name UNIQUE (name);


--
-- Name: preview_environment uq_preview_environment_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preview_environment
    ADD CONSTRAINT uq_preview_environment_name UNIQUE (tenant_id, name);


--
-- Name: repo uq_repo_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.repo
    ADD CONSTRAINT uq_repo_tenant_key UNIQUE (tenant_id, key);


--
-- Name: role_permission uq_role_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT uq_role_permission UNIQUE (role, action, resource_type);


--
-- Name: rule_system uq_rule_system_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_system
    ADD CONSTRAINT uq_rule_system_tenant_key UNIQUE (tenant_id, key);


--
-- Name: scope uq_scope_workspace_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope
    ADD CONSTRAINT uq_scope_workspace_key UNIQUE (workspace_id, key);


--
-- Name: secret_holder uq_secret_holder; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT uq_secret_holder UNIQUE (secret_id, holder_principal_id);


--
-- Name: session_event uq_session_event_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_event
    ADD CONSTRAINT uq_session_event_seq UNIQUE (session_id, event_seq);


--
-- Name: session_persona uq_session_persona; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_persona
    ADD CONSTRAINT uq_session_persona UNIQUE (session_id, persona_id);


--
-- Name: session_repo uq_session_repo; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_repo
    ADD CONSTRAINT uq_session_repo UNIQUE (session_id, repo_id);


--
-- Name: tenant_mcp_capability uq_tenant_mcp_capability_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_mcp_capability
    ADD CONSTRAINT uq_tenant_mcp_capability_key UNIQUE (tenant_id, key);


--
-- Name: tool_definition uq_tool_definition_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_definition
    ADD CONSTRAINT uq_tool_definition_tenant_key UNIQUE (tenant_id, key);


--
-- Name: workspace_knowledge_attachment uq_workspace_knowledge_attachment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT uq_workspace_knowledge_attachment UNIQUE (workspace_id, knowledge_source_id);


--
-- Name: workspace_membership uq_workspace_membership; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_membership
    ADD CONSTRAINT uq_workspace_membership UNIQUE (workspace_id, principal_id);


--
-- Name: workspace uq_workspace_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace
    ADD CONSTRAINT uq_workspace_tenant_key UNIQUE (tenant_id, key);


--
-- Name: usage_record usage_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT usage_record_pkey PRIMARY KEY (id);


--
-- Name: vector_store_item vector_store_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vector_store_item
    ADD CONSTRAINT vector_store_item_pkey PRIMARY KEY (id);


--
-- Name: vocabulary_overlay vocabulary_overlay_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vocabulary_overlay
    ADD CONSTRAINT vocabulary_overlay_pkey PRIMARY KEY (id);


--
-- Name: workflow workflow_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow
    ADD CONSTRAINT workflow_pkey PRIMARY KEY (id);


--
-- Name: workspace_knowledge_attachment workspace_knowledge_attachment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT workspace_knowledge_attachment_pkey PRIMARY KEY (id);


--
-- Name: workspace_membership workspace_membership_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_membership
    ADD CONSTRAINT workspace_membership_pkey PRIMARY KEY (id);


--
-- Name: workspace workspace_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace
    ADD CONSTRAINT workspace_pkey PRIMARY KEY (id);


--
-- Name: ix_action_record_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_record_session ON public.action_record USING btree (session_id);


--
-- Name: ix_audit_log_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_tenant_created ON public.audit_log USING btree (tenant_id, created_at);


--
-- Name: ix_await_state_pending_reminder; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_await_state_pending_reminder ON public.await_state USING btree (reminder_at) WHERE ((outcome IS NULL) AND (reminder_at IS NOT NULL));


--
-- Name: ix_await_state_pending_timeout; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_await_state_pending_timeout ON public.await_state USING btree (timeout_at) WHERE (outcome IS NULL);


--
-- Name: ix_await_state_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_await_state_session ON public.await_state USING btree (session_id);


--
-- Name: ix_checkpoint_session_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_checkpoint_session_created ON public.checkpoint USING btree (session_id, created_at);


--
-- Name: ix_context_manifest_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_context_manifest_session ON public.context_manifest USING btree (session_id);


--
-- Name: ix_entity_origin_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_origin_session ON public.entity USING btree (tenant_id, origin_session_id);


--
-- Name: ix_entity_schedule_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_schedule_workspace ON public.entity_schedule USING btree (workspace_id);


--
-- Name: ix_entity_state_change_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entity_state_change_entity_id ON public.entity_state_change USING btree (entity_id);


--
-- Name: ix_job_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_kind ON public.job USING btree (kind);


--
-- Name: ix_job_status_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_status_created_at ON public.job USING btree (status, created_at);


--
-- Name: ix_knowledge_chunk_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_chunk_embedding ON public.knowledge_chunk USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: ix_knowledge_chunk_tenant_scope_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_chunk_tenant_scope_class ON public.knowledge_chunk USING btree (tenant_id, scope_key, class);


--
-- Name: ix_knowledge_chunk_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_chunk_tsv ON public.knowledge_chunk USING gin (tsv);


--
-- Name: ix_knowledge_entry_quarantined; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_entry_quarantined ON public.knowledge_entry USING btree (tenant_id) WHERE quarantined;


--
-- Name: ix_mcp_call_record_session_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_call_record_session_server ON public.mcp_call_record USING btree (session_id, server_key);


--
-- Name: ix_mcp_server_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_server_workspace ON public.mcp_server USING btree (workspace_id);


--
-- Name: ix_message_session_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_message_session_seq ON public.message USING btree (session_id, event_seq);


--
-- Name: ix_notification_principal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_principal ON public.notification USING btree (principal_id);


--
-- Name: ix_persona_git_credential_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_persona_git_credential_repo ON public.persona_git_credential USING btree (repo_id);


--
-- Name: ix_preview_environment_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preview_environment_expiry ON public.preview_environment USING btree (tenant_id, status, expires_at);


--
-- Name: ix_provider_credential_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_provider_credential_tenant ON public.provider_credential USING btree (tenant_id);


--
-- Name: ix_registration_request_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_registration_request_tenant_status ON public.registration_request USING btree (tenant_id, status);


--
-- Name: ix_report_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_session ON public.report USING btree (session_id);


--
-- Name: ix_report_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_report_target ON public.report USING btree (generated_for_principal_id);


--
-- Name: ix_scope_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scope_workspace ON public.scope USING btree (workspace_id);


--
-- Name: ix_secret_gist_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_secret_gist_embedding ON public.secret USING hnsw (gist_embedding public.vector_cosine_ops);


--
-- Name: ix_secret_tenant_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_secret_tenant_scope ON public.secret USING btree (tenant_id, scope_key, subject_kind);


--
-- Name: ix_session_event_session_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_session_event_session_seq ON public.session_event USING btree (session_id, event_seq);


--
-- Name: ix_session_persona_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_session_persona_session ON public.session_persona USING btree (session_id);


--
-- Name: ix_usage_record_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_record_session ON public.usage_record USING btree (session_id);


--
-- Name: ix_usage_record_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_record_tenant_created ON public.usage_record USING btree (tenant_id, created_at);


--
-- Name: ix_vector_store_item_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_store_item_embedding ON public.vector_store_item USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: ix_vector_store_item_tenant_scope_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_store_item_tenant_scope_class ON public.vector_store_item USING btree (tenant_id, scope_key, class);


--
-- Name: uq_knowledge_entry_draft_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_knowledge_entry_draft_key ON public.knowledge_entry USING btree (knowledge_source_id, entry_key) WHERE (version_id IS NULL);


--
-- Name: uq_knowledge_entry_published_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_knowledge_entry_published_key ON public.knowledge_entry USING btree (version_id, entry_key) WHERE (version_id IS NOT NULL);


--
-- Name: uq_process_definition_tenant_template_key_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_process_definition_tenant_template_key_version ON public.process_definition USING btree (tenant_id, key, version) WHERE (workspace_id IS NULL);


--
-- Name: uq_process_definition_workspace_key_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_process_definition_workspace_key_version ON public.process_definition USING btree (workspace_id, key, version) WHERE (workspace_id IS NOT NULL);


--
-- Name: uq_registration_request_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_registration_request_pending ON public.registration_request USING btree (tenant_id, lower((email)::text)) WHERE ((status)::text = 'pending'::text);


--
-- Name: uq_resolution_record_session_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_resolution_record_session_seq ON public.resolution_record USING btree (session_id, event_seq);


--
-- Name: uq_vocabulary_overlay_system_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_vocabulary_overlay_system_key ON public.vocabulary_overlay USING btree (key) WHERE (tenant_id IS NULL);


--
-- Name: uq_vocabulary_overlay_tenant_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_vocabulary_overlay_tenant_key ON public.vocabulary_overlay USING btree (tenant_id, key) WHERE (tenant_id IS NOT NULL);


--
-- Name: uq_workflow_system_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_workflow_system_key ON public.workflow USING btree (key) WHERE (tenant_id IS NULL);


--
-- Name: uq_workflow_tenant_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_workflow_tenant_key ON public.workflow USING btree (tenant_id, key) WHERE (tenant_id IS NOT NULL);


--
-- Name: action_record action_record_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_record
    ADD CONSTRAINT action_record_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: action_record action_record_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_record
    ADD CONSTRAINT action_record_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: persona agent_model_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona
    ADD CONSTRAINT agent_model_profile_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id);


--
-- Name: persona_version agent_persona_version_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_version
    ADD CONSTRAINT agent_persona_version_agent_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: persona_version agent_persona_version_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_version
    ADD CONSTRAINT agent_persona_version_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: persona_version agent_persona_version_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_version
    ADD CONSTRAINT agent_persona_version_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: persona agent_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona
    ADD CONSTRAINT agent_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: persona agent_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona
    ADD CONSTRAINT agent_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: persona agent_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona
    ADD CONSTRAINT agent_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: audit_log audit_log_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: await_state await_state_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.await_state
    ADD CONSTRAINT await_state_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: await_state await_state_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.await_state
    ADD CONSTRAINT await_state_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: axis_definition axis_definition_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.axis_definition
    ADD CONSTRAINT axis_definition_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: behavior_profile behavior_profile_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavior_profile
    ADD CONSTRAINT behavior_profile_agent_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: behavior_profile behavior_profile_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavior_profile
    ADD CONSTRAINT behavior_profile_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: behavior_profile behavior_profile_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavior_profile
    ADD CONSTRAINT behavior_profile_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: checkpoint checkpoint_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint
    ADD CONSTRAINT checkpoint_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: checkpoint checkpoint_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint
    ADD CONSTRAINT checkpoint_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: completed_operation completed_operation_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.completed_operation
    ADD CONSTRAINT completed_operation_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: context_manifest context_manifest_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_manifest
    ADD CONSTRAINT context_manifest_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: context_manifest context_manifest_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_manifest
    ADD CONSTRAINT context_manifest_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: disclosure_decision disclosure_decision_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_decision
    ADD CONSTRAINT disclosure_decision_agent_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: disclosure_decision disclosure_decision_model_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_decision
    ADD CONSTRAINT disclosure_decision_model_profile_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE SET NULL;


--
-- Name: disclosure_decision disclosure_decision_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_decision
    ADD CONSTRAINT disclosure_decision_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: disclosure_decision disclosure_decision_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_decision
    ADD CONSTRAINT disclosure_decision_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity entity_origin_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_origin_session_id_fkey FOREIGN KEY (origin_session_id) REFERENCES public.session(id) ON DELETE SET NULL;


--
-- Name: entity_schedule entity_schedule_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT entity_schedule_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: entity_schedule entity_schedule_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT entity_schedule_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(id) ON DELETE CASCADE;


--
-- Name: entity_schedule entity_schedule_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT entity_schedule_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity_schedule entity_schedule_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schedule
    ADD CONSTRAINT entity_schedule_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: entity entity_schema_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_schema_id_fkey FOREIGN KEY (schema_id) REFERENCES public.entity_schema(id) ON DELETE RESTRICT;


--
-- Name: entity_schema entity_schema_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schema
    ADD CONSTRAINT entity_schema_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity_schema entity_schema_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schema
    ADD CONSTRAINT entity_schema_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: entity_state_change entity_state_change_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_state_change
    ADD CONSTRAINT entity_state_change_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE SET NULL;


--
-- Name: entity_state_change entity_state_change_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_state_change
    ADD CONSTRAINT entity_state_change_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity entity_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity entity_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: entry_activation_state entry_activation_state_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entry_activation_state
    ADD CONSTRAINT entry_activation_state_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.knowledge_entry(id) ON DELETE CASCADE;


--
-- Name: entry_activation_state entry_activation_state_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entry_activation_state
    ADD CONSTRAINT entry_activation_state_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: entry_activation_state entry_activation_state_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entry_activation_state
    ADD CONSTRAINT entry_activation_state_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: exec_environment exec_environment_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exec_environment
    ADD CONSTRAINT exec_environment_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: exec_environment exec_environment_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exec_environment
    ADD CONSTRAINT exec_environment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: entity_schema fk_entity_schema_created_by_principal; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_schema
    ADD CONSTRAINT fk_entity_schema_created_by_principal FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: entity_state_change fk_entity_state_change_entity_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_state_change
    ADD CONSTRAINT fk_entity_state_change_entity_id FOREIGN KEY (entity_id) REFERENCES public.entity(id) ON DELETE CASCADE;


--
-- Name: knowledge_entry fk_knowledge_entry_quarantine_reviewer; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_entry
    ADD CONSTRAINT fk_knowledge_entry_quarantine_reviewer FOREIGN KEY (quarantine_reviewed_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: knowledge_source fk_knowledge_source_current_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source
    ADD CONSTRAINT fk_knowledge_source_current_version FOREIGN KEY (current_version_id) REFERENCES public.knowledge_source_version(id) ON DELETE SET NULL;


--
-- Name: message fk_message_context_manifest; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message
    ADD CONSTRAINT fk_message_context_manifest FOREIGN KEY (context_manifest_id) REFERENCES public.context_manifest(id) ON DELETE SET NULL;


--
-- Name: message fk_message_overridden_by_principal; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message
    ADD CONSTRAINT fk_message_overridden_by_principal FOREIGN KEY (overridden_by_principal_id) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: agent fk_model_profile_fallback; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT fk_model_profile_fallback FOREIGN KEY (fallback_agent_id) REFERENCES public.agent(id) ON DELETE SET NULL;


--
-- Name: session fk_session_forked_from_checkpoint; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT fk_session_forked_from_checkpoint FOREIGN KEY (forked_from_checkpoint_id) REFERENCES public.checkpoint(id) ON DELETE SET NULL;


--
-- Name: session fk_session_process_definition; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT fk_session_process_definition FOREIGN KEY (process_definition_id) REFERENCES public.process_definition(id) ON DELETE SET NULL;


--
-- Name: usage_record fk_usage_record_message_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT fk_usage_record_message_id FOREIGN KEY (message_id) REFERENCES public.message(id) ON DELETE SET NULL;


--
-- Name: workflow fk_workflow_tenant; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow
    ADD CONSTRAINT fk_workflow_tenant FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: workspace fk_workspace_vocabulary_overlay; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace
    ADD CONSTRAINT fk_workspace_vocabulary_overlay FOREIGN KEY (vocabulary_overlay_id) REFERENCES public.vocabulary_overlay(id) ON DELETE SET NULL;


--
-- Name: identity identity_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity
    ADD CONSTRAINT identity_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: identity identity_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity
    ADD CONSTRAINT identity_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: knowledge_chunk knowledge_chunk_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.knowledge_entry(id) ON DELETE CASCADE;


--
-- Name: knowledge_chunk knowledge_chunk_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: knowledge_chunk knowledge_chunk_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.knowledge_source_version(id) ON DELETE CASCADE;


--
-- Name: knowledge_entry knowledge_entry_knowledge_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_entry
    ADD CONSTRAINT knowledge_entry_knowledge_source_id_fkey FOREIGN KEY (knowledge_source_id) REFERENCES public.knowledge_source(id) ON DELETE CASCADE;


--
-- Name: knowledge_entry knowledge_entry_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_entry
    ADD CONSTRAINT knowledge_entry_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: knowledge_entry knowledge_entry_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_entry
    ADD CONSTRAINT knowledge_entry_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.knowledge_source_version(id) ON DELETE CASCADE;


--
-- Name: knowledge_source knowledge_source_owner_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source
    ADD CONSTRAINT knowledge_source_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: knowledge_source knowledge_source_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source
    ADD CONSTRAINT knowledge_source_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: knowledge_source_version knowledge_source_version_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT knowledge_source_version_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: knowledge_source_version knowledge_source_version_knowledge_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT knowledge_source_version_knowledge_source_id_fkey FOREIGN KEY (knowledge_source_id) REFERENCES public.knowledge_source(id) ON DELETE CASCADE;


--
-- Name: knowledge_source_version knowledge_source_version_parent_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT knowledge_source_version_parent_version_id_fkey FOREIGN KEY (parent_version_id) REFERENCES public.knowledge_source_version(id) ON DELETE SET NULL;


--
-- Name: knowledge_source_version knowledge_source_version_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_source_version
    ADD CONSTRAINT knowledge_source_version_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: mcp_call_record mcp_call_record_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_call_record
    ADD CONSTRAINT mcp_call_record_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: mcp_call_record mcp_call_record_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_call_record
    ADD CONSTRAINT mcp_call_record_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: mcp_server mcp_server_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server
    ADD CONSTRAINT mcp_server_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: mcp_server mcp_server_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server
    ADD CONSTRAINT mcp_server_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: membership membership_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: membership membership_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: message message_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message
    ADD CONSTRAINT message_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: message message_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message
    ADD CONSTRAINT message_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: agent model_profile_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT model_profile_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: notification notification_await_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_await_state_id_fkey FOREIGN KEY (await_state_id) REFERENCES public.await_state(id) ON DELETE CASCADE;


--
-- Name: notification notification_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: notification notification_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: notification notification_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: persona_git_credential persona_git_credential_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_git_credential
    ADD CONSTRAINT persona_git_credential_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: persona_git_credential persona_git_credential_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_git_credential
    ADD CONSTRAINT persona_git_credential_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.repo(id) ON DELETE CASCADE;


--
-- Name: persona_git_credential persona_git_credential_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_git_credential
    ADD CONSTRAINT persona_git_credential_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: preview_environment preview_environment_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preview_environment
    ADD CONSTRAINT preview_environment_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE SET NULL;


--
-- Name: preview_environment preview_environment_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preview_environment
    ADD CONSTRAINT preview_environment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: principal principal_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.principal
    ADD CONSTRAINT principal_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: process_definition process_definition_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_definition
    ADD CONSTRAINT process_definition_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: process_definition process_definition_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_definition
    ADD CONSTRAINT process_definition_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: process_definition process_definition_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_definition
    ADD CONSTRAINT process_definition_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: provider_credential provider_credential_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_credential
    ADD CONSTRAINT provider_credential_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: registration_request registration_request_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registration_request
    ADD CONSTRAINT registration_request_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: repo repo_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.repo
    ADD CONSTRAINT repo_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: report report_generated_for_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report
    ADD CONSTRAINT report_generated_for_principal_id_fkey FOREIGN KEY (generated_for_principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: report report_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report
    ADD CONSTRAINT report_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: report report_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report
    ADD CONSTRAINT report_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: report report_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report
    ADD CONSTRAINT report_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: resolution_record resolution_record_rule_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resolution_record
    ADD CONSTRAINT resolution_record_rule_system_id_fkey FOREIGN KEY (rule_system_id) REFERENCES public.rule_system(id);


--
-- Name: resolution_record resolution_record_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resolution_record
    ADD CONSTRAINT resolution_record_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: resolution_record resolution_record_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resolution_record
    ADD CONSTRAINT resolution_record_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: rule_system rule_system_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rule_system
    ADD CONSTRAINT rule_system_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: scope scope_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope
    ADD CONSTRAINT scope_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: scope scope_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scope
    ADD CONSTRAINT scope_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: secret secret_authored_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret
    ADD CONSTRAINT secret_authored_by_fkey FOREIGN KEY (authored_by) REFERENCES public.principal(id) ON DELETE SET NULL;


--
-- Name: secret_disclosure_event secret_disclosure_event_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES public.disclosure_decision(id) ON DELETE SET NULL;


--
-- Name: secret_disclosure_event secret_disclosure_event_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.message(id) ON DELETE SET NULL;


--
-- Name: secret_disclosure_event secret_disclosure_event_secret_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_secret_id_fkey FOREIGN KEY (secret_id) REFERENCES public.secret(id) ON DELETE CASCADE;


--
-- Name: secret_disclosure_event secret_disclosure_event_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: secret_disclosure_event secret_disclosure_event_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_disclosure_event
    ADD CONSTRAINT secret_disclosure_event_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: secret_holder secret_holder_acquired_via_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT secret_holder_acquired_via_event_id_fkey FOREIGN KEY (acquired_via_event_id) REFERENCES public.session_event(id) ON DELETE SET NULL;


--
-- Name: secret_holder secret_holder_holder_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT secret_holder_holder_principal_id_fkey FOREIGN KEY (holder_principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: secret_holder secret_holder_secret_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT secret_holder_secret_id_fkey FOREIGN KEY (secret_id) REFERENCES public.secret(id) ON DELETE CASCADE;


--
-- Name: secret_holder secret_holder_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_holder
    ADD CONSTRAINT secret_holder_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: secret secret_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret
    ADD CONSTRAINT secret_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: secret secret_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret
    ADD CONSTRAINT secret_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: session session_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_agent_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: session_event session_event_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_event
    ADD CONSTRAINT session_event_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: session_event session_event_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_event
    ADD CONSTRAINT session_event_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: session_persona session_persona_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_persona
    ADD CONSTRAINT session_persona_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona(id) ON DELETE CASCADE;


--
-- Name: session_persona session_persona_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_persona
    ADD CONSTRAINT session_persona_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: session_persona session_persona_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_persona
    ADD CONSTRAINT session_persona_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: session_repo session_repo_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_repo
    ADD CONSTRAINT session_repo_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.repo(id) ON DELETE CASCADE;


--
-- Name: session_repo session_repo_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_repo
    ADD CONSTRAINT session_repo_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.session(id) ON DELETE CASCADE;


--
-- Name: session_repo session_repo_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_repo
    ADD CONSTRAINT session_repo_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: session session_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: session session_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session
    ADD CONSTRAINT session_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: tenant_mcp_capability tenant_mcp_capability_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_mcp_capability
    ADD CONSTRAINT tenant_mcp_capability_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: tool_definition tool_definition_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_definition
    ADD CONSTRAINT tool_definition_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: usage_record usage_record_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT usage_record_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: vocabulary_overlay vocabulary_overlay_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vocabulary_overlay
    ADD CONSTRAINT vocabulary_overlay_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: workspace_knowledge_attachment workspace_knowledge_attachment_knowledge_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT workspace_knowledge_attachment_knowledge_source_id_fkey FOREIGN KEY (knowledge_source_id) REFERENCES public.knowledge_source(id) ON DELETE CASCADE;


--
-- Name: workspace_knowledge_attachment workspace_knowledge_attachment_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT workspace_knowledge_attachment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: workspace_knowledge_attachment workspace_knowledge_attachment_version_pin_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT workspace_knowledge_attachment_version_pin_fkey FOREIGN KEY (version_pin) REFERENCES public.knowledge_source_version(id) ON DELETE SET NULL;


--
-- Name: workspace_knowledge_attachment workspace_knowledge_attachment_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_knowledge_attachment
    ADD CONSTRAINT workspace_knowledge_attachment_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: workspace_membership workspace_membership_principal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_membership
    ADD CONSTRAINT workspace_membership_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.principal(id) ON DELETE CASCADE;


--
-- Name: workspace_membership workspace_membership_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_membership
    ADD CONSTRAINT workspace_membership_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: workspace_membership workspace_membership_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_membership
    ADD CONSTRAINT workspace_membership_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace(id) ON DELETE CASCADE;


--
-- Name: workspace workspace_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace
    ADD CONSTRAINT workspace_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE CASCADE;


--
-- Name: action_record; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.action_record ENABLE ROW LEVEL SECURITY;

--
-- Name: agent; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;

--
-- Name: await_state; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.await_state ENABLE ROW LEVEL SECURITY;

--
-- Name: axis_definition; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.axis_definition ENABLE ROW LEVEL SECURITY;

--
-- Name: behavior_profile; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.behavior_profile ENABLE ROW LEVEL SECURITY;

--
-- Name: checkpoint; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.checkpoint ENABLE ROW LEVEL SECURITY;

--
-- Name: completed_operation; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.completed_operation ENABLE ROW LEVEL SECURITY;

--
-- Name: context_manifest; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.context_manifest ENABLE ROW LEVEL SECURITY;

--
-- Name: disclosure_decision; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.disclosure_decision ENABLE ROW LEVEL SECURITY;

--
-- Name: entity; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entity ENABLE ROW LEVEL SECURITY;

--
-- Name: entity_schedule; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entity_schedule ENABLE ROW LEVEL SECURITY;

--
-- Name: entity_schema; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entity_schema ENABLE ROW LEVEL SECURITY;

--
-- Name: entity_state_change; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entity_state_change ENABLE ROW LEVEL SECURITY;

--
-- Name: entry_activation_state; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.entry_activation_state ENABLE ROW LEVEL SECURITY;

--
-- Name: exec_environment; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.exec_environment ENABLE ROW LEVEL SECURITY;

--
-- Name: identity; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.identity ENABLE ROW LEVEL SECURITY;

--
-- Name: knowledge_chunk; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.knowledge_chunk ENABLE ROW LEVEL SECURITY;

--
-- Name: knowledge_entry; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.knowledge_entry ENABLE ROW LEVEL SECURITY;

--
-- Name: knowledge_source; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.knowledge_source ENABLE ROW LEVEL SECURITY;

--
-- Name: knowledge_source_version; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.knowledge_source_version ENABLE ROW LEVEL SECURITY;

--
-- Name: mcp_call_record; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_call_record ENABLE ROW LEVEL SECURITY;

--
-- Name: mcp_server; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_server ENABLE ROW LEVEL SECURITY;

--
-- Name: membership; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.membership ENABLE ROW LEVEL SECURITY;

--
-- Name: message; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.message ENABLE ROW LEVEL SECURITY;

--
-- Name: notification; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification ENABLE ROW LEVEL SECURITY;

--
-- Name: persona; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.persona ENABLE ROW LEVEL SECURITY;

--
-- Name: persona_git_credential; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.persona_git_credential ENABLE ROW LEVEL SECURITY;

--
-- Name: persona_version; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.persona_version ENABLE ROW LEVEL SECURITY;

--
-- Name: preview_environment; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.preview_environment ENABLE ROW LEVEL SECURITY;

--
-- Name: principal; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.principal ENABLE ROW LEVEL SECURITY;

--
-- Name: process_definition; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.process_definition ENABLE ROW LEVEL SECURITY;

--
-- Name: provider_credential; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.provider_credential ENABLE ROW LEVEL SECURITY;

--
-- Name: registration_request; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.registration_request ENABLE ROW LEVEL SECURITY;

--
-- Name: repo; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.repo ENABLE ROW LEVEL SECURITY;

--
-- Name: report; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.report ENABLE ROW LEVEL SECURITY;

--
-- Name: resolution_record; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.resolution_record ENABLE ROW LEVEL SECURITY;

--
-- Name: rule_system; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.rule_system ENABLE ROW LEVEL SECURITY;

--
-- Name: scope; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scope ENABLE ROW LEVEL SECURITY;

--
-- Name: secret; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.secret ENABLE ROW LEVEL SECURITY;

--
-- Name: secret_disclosure_event; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.secret_disclosure_event ENABLE ROW LEVEL SECURITY;

--
-- Name: secret_holder; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.secret_holder ENABLE ROW LEVEL SECURITY;

--
-- Name: session; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session ENABLE ROW LEVEL SECURITY;

--
-- Name: session_event; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session_event ENABLE ROW LEVEL SECURITY;

--
-- Name: session_persona; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session_persona ENABLE ROW LEVEL SECURITY;

--
-- Name: session_repo; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session_repo ENABLE ROW LEVEL SECURITY;

--
-- Name: action_record tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.action_record USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: agent tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.agent USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: audit_log tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.audit_log USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: await_state tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.await_state USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: axis_definition tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.axis_definition USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: behavior_profile tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.behavior_profile USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: checkpoint tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.checkpoint USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: completed_operation tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.completed_operation USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: context_manifest tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.context_manifest USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: disclosure_decision tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.disclosure_decision USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: entity tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.entity USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: entity_schedule tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.entity_schedule USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: entity_schema tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.entity_schema USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: entity_state_change tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.entity_state_change USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: entry_activation_state tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.entry_activation_state USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: exec_environment tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.exec_environment USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: identity tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.identity USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: knowledge_chunk tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.knowledge_chunk USING (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) OR (tenant_id = '00000000-0000-0000-0000-000000000001'::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: knowledge_entry tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.knowledge_entry USING (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) OR (tenant_id = '00000000-0000-0000-0000-000000000001'::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: knowledge_source tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.knowledge_source USING (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) OR (tenant_id = '00000000-0000-0000-0000-000000000001'::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: knowledge_source_version tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.knowledge_source_version USING (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) OR (tenant_id = '00000000-0000-0000-0000-000000000001'::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: mcp_call_record tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.mcp_call_record USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: mcp_server tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.mcp_server USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: membership tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.membership USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: message tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.message USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: notification tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.notification USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: persona tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.persona USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: persona_git_credential tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.persona_git_credential USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: persona_version tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.persona_version USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: preview_environment tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.preview_environment USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: principal tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.principal USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: process_definition tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.process_definition USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: provider_credential tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.provider_credential USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: registration_request tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.registration_request USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: repo tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.repo USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: report tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.report USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: resolution_record tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.resolution_record USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: rule_system tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.rule_system USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: scope tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.scope USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: secret tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.secret USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: secret_disclosure_event tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.secret_disclosure_event USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: secret_holder tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.secret_holder USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: session tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.session USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: session_event tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.session_event USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: session_persona tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.session_persona USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: session_repo tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.session_repo USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: tenant_mcp_capability tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenant_mcp_capability USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: tool_definition tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tool_definition USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: usage_record tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.usage_record USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: vector_store_item tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.vector_store_item USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: vocabulary_overlay tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.vocabulary_overlay USING (((tenant_id IS NULL) OR (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: workflow tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.workflow USING (((tenant_id IS NULL) OR (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid))) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: workspace tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.workspace USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: workspace_knowledge_attachment tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.workspace_knowledge_attachment USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: workspace_membership tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.workspace_membership USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: tenant_mcp_capability; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenant_mcp_capability ENABLE ROW LEVEL SECURITY;

--
-- Name: tool_definition; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tool_definition ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_record; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.usage_record ENABLE ROW LEVEL SECURITY;

--
-- Name: vector_store_item; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.vector_store_item ENABLE ROW LEVEL SECURITY;

--
-- Name: vocabulary_overlay; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.vocabulary_overlay ENABLE ROW LEVEL SECURITY;

--
-- Name: workflow; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workflow ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_knowledge_attachment; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_knowledge_attachment ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_membership; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_membership ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO pyrrhula_app;


--
-- Name: TABLE action_record; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.action_record TO pyrrhula_app;


--
-- Name: TABLE agent; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent TO pyrrhula_app;


--
-- Name: TABLE audit_log; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.audit_log TO pyrrhula_app;


--
-- Name: TABLE await_state; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.await_state TO pyrrhula_app;


--
-- Name: TABLE axis_definition; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.axis_definition TO pyrrhula_app;


--
-- Name: TABLE behavior_profile; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.behavior_profile TO pyrrhula_app;


--
-- Name: TABLE checkpoint; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.checkpoint TO pyrrhula_app;


--
-- Name: TABLE completed_operation; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.completed_operation TO pyrrhula_app;


--
-- Name: TABLE context_manifest; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.context_manifest TO pyrrhula_app;


--
-- Name: TABLE deployment_setting; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deployment_setting TO pyrrhula_app;


--
-- Name: TABLE disclosure_decision; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.disclosure_decision TO pyrrhula_app;


--
-- Name: TABLE entity; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.entity TO pyrrhula_app;


--
-- Name: TABLE entity_schedule; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.entity_schedule TO pyrrhula_app;


--
-- Name: TABLE entity_schema; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.entity_schema TO pyrrhula_app;


--
-- Name: TABLE entity_state_change; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.entity_state_change TO pyrrhula_app;


--
-- Name: TABLE entry_activation_state; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.entry_activation_state TO pyrrhula_app;


--
-- Name: TABLE exec_environment; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.exec_environment TO pyrrhula_app;


--
-- Name: TABLE identity; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.identity TO pyrrhula_app;


--
-- Name: TABLE job; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.job TO pyrrhula_app;


--
-- Name: TABLE knowledge_chunk; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.knowledge_chunk TO pyrrhula_app;


--
-- Name: TABLE knowledge_entry; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.knowledge_entry TO pyrrhula_app;


--
-- Name: TABLE knowledge_source; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.knowledge_source TO pyrrhula_app;


--
-- Name: TABLE knowledge_source_version; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.knowledge_source_version TO pyrrhula_app;


--
-- Name: TABLE mcp_call_record; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.mcp_call_record TO pyrrhula_app;


--
-- Name: TABLE mcp_server; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_server TO pyrrhula_app;


--
-- Name: TABLE membership; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.membership TO pyrrhula_app;


--
-- Name: TABLE message; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.message TO pyrrhula_app;


--
-- Name: TABLE model_capability; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_capability TO pyrrhula_app;


--
-- Name: TABLE notification; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification TO pyrrhula_app;


--
-- Name: TABLE persona; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.persona TO pyrrhula_app;


--
-- Name: TABLE persona_git_credential; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.persona_git_credential TO pyrrhula_app;


--
-- Name: TABLE persona_version; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.persona_version TO pyrrhula_app;


--
-- Name: TABLE plugin_repository; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.plugin_repository TO pyrrhula_app;


--
-- Name: TABLE preview_environment; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.preview_environment TO pyrrhula_app;


--
-- Name: TABLE price_table; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.price_table TO pyrrhula_app;


--
-- Name: TABLE principal; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.principal TO pyrrhula_app;


--
-- Name: TABLE process_definition; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.process_definition TO pyrrhula_app;


--
-- Name: TABLE provider_credential; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_credential TO pyrrhula_app;


--
-- Name: TABLE registration_request; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.registration_request TO pyrrhula_app;


--
-- Name: TABLE repo; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.repo TO pyrrhula_app;


--
-- Name: TABLE report; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.report TO pyrrhula_app;


--
-- Name: TABLE resolution_record; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.resolution_record TO pyrrhula_app;


--
-- Name: TABLE role_permission; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.role_permission TO pyrrhula_app;


--
-- Name: TABLE rule_system; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rule_system TO pyrrhula_app;


--
-- Name: TABLE scope; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scope TO pyrrhula_app;


--
-- Name: TABLE secret; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secret TO pyrrhula_app;


--
-- Name: TABLE secret_disclosure_event; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.secret_disclosure_event TO pyrrhula_app;


--
-- Name: TABLE secret_holder; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secret_holder TO pyrrhula_app;


--
-- Name: TABLE session; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session TO pyrrhula_app;


--
-- Name: TABLE session_event; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.session_event TO pyrrhula_app;


--
-- Name: TABLE session_persona; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_persona TO pyrrhula_app;


--
-- Name: TABLE session_repo; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.session_repo TO pyrrhula_app;


--
-- Name: TABLE tenant; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tenant TO pyrrhula_app;


--
-- Name: TABLE tenant_mcp_capability; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tenant_mcp_capability TO pyrrhula_app;


--
-- Name: TABLE tool_definition; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tool_definition TO pyrrhula_app;


--
-- Name: TABLE usage_record; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.usage_record TO pyrrhula_app;


--
-- Name: TABLE vector_store_item; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.vector_store_item TO pyrrhula_app;


--
-- Name: TABLE vocabulary_overlay; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.vocabulary_overlay TO pyrrhula_app;


--
-- Name: TABLE workflow; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workflow TO pyrrhula_app;


--
-- Name: TABLE workspace; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workspace TO pyrrhula_app;


--
-- Name: TABLE workspace_knowledge_attachment; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workspace_knowledge_attachment TO pyrrhula_app;


--
-- Name: TABLE workspace_membership; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workspace_membership TO pyrrhula_app;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE pyrrhula IN SCHEMA public GRANT SELECT,USAGE ON SEQUENCES TO pyrrhula_app;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE pyrrhula IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO pyrrhula_app;


--
-- PostgreSQL database dump complete
--


