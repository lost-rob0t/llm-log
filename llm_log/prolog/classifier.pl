:- use_module(library(http/json)).
:- use_module(library(lists)).

keyword(coding, "implement").
keyword(coding, "code").
keyword(coding, "fix").
keyword(coding, "bug").
keyword(coding, "test").
keyword(coding, "compile").
keyword(coding, "repo").
keyword(research, "research").
keyword(research, "investigate").
keyword(research, "source").
keyword(research, "evidence").
keyword(search, "search").
keyword(search, "find").
keyword(search, "lookup").
keyword(search, "look up").
keyword(writing, "write").
keyword(writing, "rewrite").
keyword(writing, "draft").
keyword(writing, "email").
keyword(analysis, "analyze").
keyword(analysis, "analyse").
keyword(analysis, "compare").
keyword(analysis, "review").

contains_keyword(Text, Intent) :-
    keyword(Intent, Keyword),
    sub_string(Text, _, _, _, Keyword).

classify_text(Input, Labels) :-
    string_lower(Input, Lower),
    findall(Intent, contains_keyword(Lower, Intent), Raw),
    sort(Raw, Unique),
    ( Unique = [] -> Labels = [chat] ; Labels = Unique ).

main :-
    read_string(user_input, _, Input),
    classify_text(Input, Labels),
    json_write(current_output, Labels, [width(0)]),
    nl.
