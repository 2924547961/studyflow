"""Mechanical review-schema normalization, never answer generation or evidence invention."""
from __future__ import annotations
import copy


def normalize_reviews(questions,answers,reviews):
    from .quiz import _validate_reviews,validate_answer_set,lookup_verified_answers
    if len(reviews)!=len(questions):raise ValueError('Reviews must match all observed questions')
    normalized=validate_answer_set([q['type_hint'] for q in questions],answers,
                                  [q.get('required_selection_count') for q in questions])
    result=copy.deepcopy(reviews)
    cached=lookup_verified_answers(questions)['answers']
    for q,answer,review,confirmed in zip(questions,normalized,result,cached):
        # Server-side exact platform confirmation already satisfies this review;
        # the final validator still checks fingerprints, rejects and conflicts.
        if confirmed==answer:continue
        # Never turn unreviewed/uncertain input into a certain reviewed answer.
        if review.get('model_reviewed') is not True or review.get('certain') is not True:
            raise ValueError('Explicit current-model review and certainty required')
        if q['type_hint']!='multiple_choice':continue
        raw=review.get('option_checks')
        if isinstance(raw,list):
            converted={}
            for item in raw:
                if not isinstance(item,dict):raise ValueError('Each option check requires its letter and evidence')
                letter=item.get('letter',item.get('option'))
                if letter in converted or not isinstance(letter,str):raise ValueError('Duplicate or missing option-check letter')
                converted[letter]={k:v for k,v in item.items() if k not in ('letter','option')}
            raw=converted
        if not isinstance(raw,dict):raise ValueError('Option checks must cover every observed option')
        review['option_checks']=raw
        # These are schema semantics, not inferred answers or added evidence.
        for letter,check in raw.items():
            if not isinstance(check,dict):raise ValueError('Each option needs a structured evidence check')
            aliases={'selected':'include','excluded':'exclude','select':'include','reject':'exclude'}
            verdict=aliases.get(check.get('verdict'),check.get('verdict'))
            expected='include' if letter in answer else 'exclude'
            if verdict is not None and verdict!=expected:raise ValueError('Reviewed answer and option verdict conflict')
            check['verdict']=expected
            check.setdefault('addresses_exact_question',True)
            check.setdefault('general_truth_only',False)
        review.setdefault('policy_version',2)
        if 'selected_reasons' not in review or isinstance(review['selected_reasons'],list):
            review['selected_reasons']={l:c.get('evidence','') for l,c in raw.items() if l in answer}
        if 'excluded_reasons' not in review or isinstance(review['excluded_reasons'],list):
            review['excluded_reasons']={l:c.get('evidence','') for l,c in raw.items() if l not in answer}
        # question_scope, evidence_kind, certainty, cross_checked, sources and
        # course locators stay explicit. The validator retains all original guards.
    _validate_reviews(questions,normalized,result)
    return normalized,result
