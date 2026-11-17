(function () {
    "use strict";

    var NAME_SELECTOR = 'input[name="first_name"], input[name="middle_name"], ' +
        'input[name="last_name"], input[name="suffix"], input[name="full_name"]';

    var SUFFIXES = {
        "jr": "Jr.",
        "jr.": "Jr.",
        "sr": "Sr.",
        "sr.": "Sr.",
        "ii": "II",
        "iii": "III",
        "iv": "IV",
        "v": "V",
        "vi": "VI"
    };

    function isNameInput(element) {
        return !!(element && element.matches && element.matches(NAME_SELECTOR));
    }

    function isLetter(char) {
        return char.toLowerCase() !== char.toUpperCase();
    }

    function hasIntentionalCasing(token) {
        // CamelCase such as "McDonald": starts uppercase, has another uppercase
        // later, and also contains lowercase letters.
        if (token.length < 2) {
            return false;
        }
        var first = token.charAt(0);
        if (first !== first.toUpperCase() || first === first.toLowerCase()) {
            return false;
        }
        var rest = token.slice(1);
        return rest.toLowerCase() !== rest && rest.toUpperCase() !== rest;
    }

    function titleCaseSegments(token) {
        var result = "";
        var atWordStart = true;
        for (var index = 0; index < token.length; index += 1) {
            var char = token.charAt(index);
            if (char === "-" || char === "'" || char === "\u2019") {
                result += char;
                atWordStart = true;
            } else if (isLetter(char)) {
                result += atWordStart ? char.toUpperCase() : char.toLowerCase();
                atWordStart = false;
            } else {
                result += char;
                atWordStart = false;
            }
        }
        return result;
    }

    function formatToken(token) {
        var suffix = SUFFIXES[token.toLowerCase()];
        if (suffix) {
            return suffix;
        }
        if (hasIntentionalCasing(token)) {
            return token;
        }
        return titleCaseSegments(token);
    }

    function formatName(value) {
        return value.replace(/\S+/g, function (token) {
            return formatToken(token);
        });
    }

    var edited = typeof WeakSet === "function" ? new WeakSet() : null;
    var editingComposition = false;

    function markEdited(input) {
        if (edited) {
            edited.add(input);
            return;
        }
        input.dataset.nameEdited = "true";
    }

    function wasEdited(input) {
        if (edited) {
            return edited.has(input);
        }
        return input.dataset.nameEdited === "true";
    }

    function commit(input) {
        if (!isNameInput(input) || input.readOnly || input.disabled) {
            return;
        }
        var formatted = formatName(input.value);
        if (formatted !== input.value) {
            input.value = formatted;
        }
    }

    function liveFormat(input) {
        // Skip while composing (IME) and only rewrite in place when the length
        // is unchanged, so the caret never jumps.
        if (editingComposition || input.readOnly || input.disabled) {
            return;
        }
        var formatted = formatName(input.value);
        if (formatted === input.value || formatted.length !== input.value.length) {
            return;
        }
        var start = null;
        var end = null;
        try {
            start = input.selectionStart;
            end = input.selectionEnd;
        } catch (error) {
            return;
        }
        input.value = formatted;
        if (start !== null && end !== null) {
            try {
                input.setSelectionRange(start, end);
            } catch (error) {
                // Input types without selection support keep their value.
            }
        }
    }

    document.addEventListener("compositionstart", function (event) {
        if (isNameInput(event.target)) {
            editingComposition = true;
        }
    }, true);

    document.addEventListener("compositionend", function (event) {
        if (isNameInput(event.target)) {
            editingComposition = false;
            commit(event.target);
        }
    }, true);

    document.addEventListener("input", function (event) {
        var input = event.target;
        if (!isNameInput(input)) {
            return;
        }
        input.setAttribute("autocapitalize", "words");
        markEdited(input);
        liveFormat(input);
    }, true);

    document.addEventListener("change", function (event) {
        if (isNameInput(event.target) && wasEdited(event.target)) {
            commit(event.target);
        }
    }, true);

    document.addEventListener("focusout", function (event) {
        if (isNameInput(event.target) && wasEdited(event.target)) {
            commit(event.target);
        }
    }, true);

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || !form.querySelectorAll) {
            return;
        }
        var inputs = form.querySelectorAll(NAME_SELECTOR);
        for (var index = 0; index < inputs.length; index += 1) {
            if (wasEdited(inputs[index])) {
                commit(inputs[index]);
            }
        }
    }, true);

    function setUpExistingInputs() {
        var inputs = document.querySelectorAll(NAME_SELECTOR);
        for (var index = 0; index < inputs.length; index += 1) {
            inputs[index].setAttribute("autocapitalize", "words");
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", setUpExistingInputs);
    } else {
        setUpExistingInputs();
    }
})();
