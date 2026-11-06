# ADR-0002: Version a source-neutral Candidate Intake contract

Date: 2026-11-03. Status: proposed.

## Context

The local sample is CSV with mixed direct, search and shop links. Spark is the intended discovery producer. Legacy Discovery and Excel/Sheet intake have different provenance.

## Decision

Ingest verified file deliveries through source adapters. Store raw evidence and a versioned normalized batch/row contract. Resolve the supplied source to a listing before crawling. Route Legacy Discovery through its own adapter and retain current controls.

## Alternatives and consequences

A direct API can lower latency once a supported interface and account delivery are verified. Parallel downstream pipelines duplicate policy and drift. A common contract retains provenance and introduces explicit mapping work and a late-input state. New transports can be added independently of catalog logic.
