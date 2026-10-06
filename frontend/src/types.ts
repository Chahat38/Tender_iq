/**
 * TypeScript mirrors of the Stage 6 API response models
 * (collector/api.py). Field names and nullability match the JSON
 * exactly; scraped text is untrusted and is only ever rendered as
 * plain text by the UI.
 */

/** One registered procurement source (SourceOut). */
export interface Source {
  id: number;
  name: string;
  adapter_type: string;
  active: number;
  robots_allowed: number | null;
  terms_checked: number | null;
  requires_authentication: number | null;
  crawl_frequency: string | null;
  last_crawled_at: string | null;
}

/** One opportunity, source-recorded fields (OpportunityOut). */
export interface Opportunity {
  id: number;
  source_id: number;
  external_id: string;
  reference_number: string | null;
  title: string | null;
  organization: string | null;
  description: string | null;
  source_url: string;
  published_at: string | null;
  deadline: string | null;
  location: string | null;
  category: string | null;
  procurement_type: string | null;
  content_hash: string;
  first_seen_at: string;
  last_seen_at: string;
  last_verified_at: string;
}

/** One document attached to an opportunity (DocumentOut). */
export interface DocumentLink {
  id: number;
  url: string;
  filename: string | null;
}

/** One recorded field change (UpdateOut). */
export interface FieldUpdate {
  id: number;
  field: string;
  old_value: string | null;
  new_value: string | null;
  detected_at: string;
  update_type: string;
}

/** One opportunity plus documents and update history (OpportunityDetailOut). */
export interface OpportunityDetail extends Opportunity {
  documents: DocumentLink[];
  updates: FieldUpdate[];
}

/** One page of opportunities (OpportunityPage). */
export interface OpportunityPage {
  total: number;
  limit: number;
  offset: number;
  opportunities: Opportunity[];
}

/** One ranked full-text search hit (SearchHitOut; lower rank = better). */
export interface SearchHit {
  opportunity_id: number;
  external_id: string;
  source_id: number;
  title: string | null;
  organization: string | null;
  snippet: string | null;
  deadline: string | null;
  location: string | null;
  category: string | null;
  procurement_type: string | null;
  reference_number: string | null;
  source_url: string;
  rank: number;
}

/** One page of search hits (SearchPage). */
export interface SearchPage {
  query: string;
  total: number;
  limit: number;
  offset: number;
  results: SearchHit[];
}
